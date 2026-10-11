"""BMSK p-code fixtures and SM89 whole-kernel GPU/reference comparisons.

SASS_SLEIGH_OUT=/tmp/... python3 tests/bmsk_semantics.py --arch SM89
python3 tests/bmsk_semantics.py --require-gpu
"""
import argparse,itertools,json,os,random,struct,subprocess,sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from sass import ir,pydecode
from semantic_cases import encode
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT


def reference(position,width,mode):
    """Enumerate the selected bits, independently of the p-code's shifts."""
    position &= 0xffffffff; width &= 0xffffffff
    if mode=='W':position &= 31; width &= 31
    elif mode!='C':raise ValueError(mode)
    return sum(1<<bit for bit in range(32) if position<=bit<position+width)


def cases(sm):
    arch=ir.load(sm);available={k.name for k in arch.classes};out=[]
    def add(cls,fields,registers,expected,**extras):
        if cls not in available:return
        request=dict(word=encode(sm,cls,**fields),registers=registers,
                     observe=[n for n in expected if not n.startswith('__')],
                     inspect_pcode=True,**extras)
        out.append((cls,request,expected))
    edges=[0,1,15,30,31,32,33,63,64,0x7fffffff,0x80000000,0xffffffff]
    rng=random.Random(18)
    varied=list(itertools.product(edges,repeat=2))+[(rng.getrandbits(32),rng.getrandbits(32)) for _ in range(32)]
    shapes=[('bmsk__RRR_RRR','R'),('bmsk__RUR_RUR','UR'),
            ('bmsk__RuIR_RIR','I'),('bmsk__RCR_RCR','C')]
    for cls,shape in shapes:
        vectors=list(itertools.product(range(34),repeat=2))+varied if shape=='R' else varied
        for mode in ('C','W'):
            for i,(a,n) in enumerate(vectors):
                rd=(2,4,8)[i%3];fields=dict(Rd=rd,Ra=2,cw=mode);regs=dict(R2=a);extras={}
                if shape=='R':fields['Rb']=4;regs['R4']=n
                elif shape=='UR':fields['URb']=4;regs['UR4']=n
                elif shape=='I':fields['Sb']=n
                else:
                    fields.update(Sb_bank=2,Sb_addr=16)
                    extras['memory']=[dict(space='cbank',address=(2<<32)|16,hex=n.to_bytes(4,'little').hex())]
                add(cls,fields,regs,{f'R{rd}':reference(a,n,mode),'__native':True},**extras)
    for mode in ('C','W'):
        for a,n in [(0,0),(31,32),(32,33),(0xffffffff,0xffffffff)]:
            add('bmsk__RRR_RRR',dict(Rd=8,Ra='RZ',Rb=4,cw=mode),dict(R4=n),dict(R8=reference(0,n,mode),__native=True))
            add('bmsk__RRR_RRR',dict(Rd=8,Ra=2,Rb='RZ',cw=mode),dict(R2=a),dict(R8=0,__native=True))
        add('bmsk__RUR_RUR',dict(Rd=8,Ra=2,URb='URZ',cw=mode),dict(R2=1),dict(R8=0,__native=True))
        add('bmsk__RRR_RRR',dict(Rd=8,Ra=2,Rb=2,cw=mode),dict(R2=31),dict(R8=reference(31,31,mode),__native=True))
        add('bmsk__RRR_RRR',dict(Rd='RZ',Ra=2,Rb=4,cw=mode),dict(R2=1,R4=2),dict(RZ=0))
        for pred,invert in itertools.product((0,1),repeat=2):
            expected=reference(1,3,mode) if pred^invert else 0xdeadbeef
            add('bmsk__RRR_RRR',dict(Rd=8,Ra=2,Rb=4,cw=mode,Pg=0,**{'Pg@not':invert}),
                dict(R2=1,R4=3,R8=0xdeadbeef,P0=pred),dict(R8=expected,__native=True))
    from bmsk_cx_semantics import cases as cx_cases
    out.extend(cx_cases(sm))
    return out


def hardware(output,require_gpu=False,compile_only=False):
    from gpu_semantics import Driver,kernel_request
    output.mkdir(parents=True,exist_ok=True);cubin=output/'bmsk_semantics.cubin'
    subprocess.run([os.environ.get('NVCC','/usr/local/cuda/bin/nvcc'),'--allow-unsupported-compiler',
        '-cubin','-arch=sm_89',str(ROOT/'tests/kernels/bmsk_semantics.cu'),'-o',str(cubin)],check=True)
    from sass.cubin import kernel_image
    images={mode:kernel_image(cubin.read_bytes(),'sem_bmsk_'+mode.lower()) for mode in ('C','W')}
    for mode,image in images.items():
        code=image['code'];targets=[]
        for offset in range(0,len(code),16):
            decoded=pydecode.decode('SM89',code[offset:offset+16])
            if decoded.klass.mnemonic=='BMSK':targets.append((offset,decoded))
        if len(targets)!=1 or targets[0][1].env['cw']!=ir.load('SM89').enums['CWMode'][mode]:
            raise RuntimeError(f'{mode}: compiler did not emit exactly one BMSK in the requested mode')
        image['target_offset']=targets[0][0]
    words={mode:int.from_bytes(image['code'][image['target_offset']:image['target_offset']+16],'little')
           for mode,image in images.items()}
    if words['C'] ^ words['W'] != 1<<75:
        raise AssertionError('Clamp/wrap fixtures must differ only at instruction bit 75')
    print('Compiled and confirmed SM89 BMSK clamp/wrap fixtures',flush=True)
    if compile_only:return
    try:driver=Driver()
    except (OSError,RuntimeError) as error:
        if require_gpu:raise
        print('SKIP GPU execution: '+str(error));return
    rng=random.Random(18);edges=[0,1,15,30,31,32,33,63,64,255,256,0x7fffffff,0x80000000,0xffffffff]
    vectors=list(itertools.product(range(34),repeat=2))+list(itertools.product(edges,repeat=2))
    vectors += [(rng.getrandbits(32),rng.getrandbits(32)) for _ in range(128)]
    vectors += [(0,0)]*((-len(vectors))%32)
    data=b''.join(struct.pack('<III',a,n,0) for a,n in vectors);requests=[];records=[]
    try:
        for mode in ('C','W'):
            outputs=driver.run(cubin,'bmsk_'+mode.lower(),data)
            expected=[reference(a,n,mode) for a,n in vectors]
            if list(outputs)!=expected:
                i=next(i for i,(got,want) in enumerate(zip(outputs,expected)) if got!=want)
                raise AssertionError((mode,i,vectors[i],hex(outputs[i]),hex(expected[i])))
            requests.append(kernel_request(cubin.read_bytes(),'bmsk_'+mode.lower(),data))
            records.append(dict(mode=mode,passed=len(outputs),target_offset=images[mode]['target_offset']))
            print(f'BMSK {mode}: {len(outputs)} GPU/reference comparisons passed',flush=True)
        process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),'SASS:LE:64:sm89'],
            input=''.join(json.dumps(r)+'\n' for r in requests),capture_output=True,text=True)
        emulated=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
        if len(emulated)!=len(records):raise RuntimeError(process.stderr[-2500:])
        for record,result in zip(records,emulated):
            if 'error' in result:raise RuntimeError(result['error'])
            if record['target_offset'] not in result['visited']:raise AssertionError('BMSK was not executed')
            actual=[int.from_bytes(bytes.fromhex(v),'little') for v in result['outputs']]
            expected=[reference(a,n,record['mode']) for a,n in vectors]
            if actual!=expected:
                i=next(i for i,(got,want) in enumerate(zip(actual,expected)) if got!=want)
                raise AssertionError((record['mode'],i,vectors[i],hex(actual[i]),hex(expected[i])))
            record.update(steps=result['steps'],visited=result['visited'])
            print(f'BMSK {record["mode"]}: {len(actual)} whole-kernel p-code comparisons passed',flush=True)
        (output/'results.json').write_text(json.dumps(dict(architecture='SM89',seed=18,mode_bit=75,
            mode='whole-kernel',reference='bit enumeration',results=records),indent=2)+'\n')
    finally:driver.close()


def main():
    p=argparse.ArgumentParser();p.add_argument('--arch',default='SM89');p.add_argument('--fp',choices=('exact','readable'),default='exact')
    p.add_argument('--require-gpu',action='store_true');p.add_argument('--compile-only',action='store_true')
    p.add_argument('--output',type=Path,default=Path('/tmp/sass-bmsk-oracle'));args=p.parse_args()
    if args.require_gpu or args.compile_only:hardware(args.output,args.require_gpu,args.compile_only)
    else:
        from test_semantics import run
        run(args.arch,args.fp,cases(args.arch))


if __name__=='__main__':main()
