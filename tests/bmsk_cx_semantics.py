"""BMSK CX values and patched SM89 GPU / whole-kernel p-code oracle.

python3 tests/bmsk_cx_semantics.py --arch SM90
python3 tests/bmsk_cx_semantics.py --require-gpu
"""
import argparse,itertools,json,os,struct,subprocess,sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from sass import cubin,pydecode
from semantic_cases import encode
from bmsk_semantics import reference
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT

CLASS='bmsk__RCxR_RCxR'
ARCHES=('SM75','SM80','SM86','SM89','SM90')
POSITIONS=list(range(34))+[0x7fffffff,0x80000000,0xffffffff]+list(range(91))
WIDTHS=[0,1,2,15,31,32,33,63,64,0x7fffffff,0x80000000,0xffffffff]


def cases(sm):
    if sm not in ARCHES:return []
    bits,shift=(51,6) if sm=='SM90' else (45,4)
    out=[]
    def add(mode,a,width,units,offset,base,ur=4,rd=8,pred=None):
        header=(base//(1<<shift))+(units<<bits)
        fields=dict(Rd=rd,Ra=2,URb=ur,Sb_offset=offset,cw=mode)
        regs={f'UR{ur}':header&0xffffffff,f'UR{ur+1}':header>>32,'R2':a}
        expected=reference(a,width if offset+4<=units*16 else 0,mode)
        if pred is not None:
            fields['Pg']=0;regs.update(P0=pred,R8=0xdeadbeef)
            if not pred:expected=0xdeadbeef
        request=dict(word=encode(sm,CLASS,**fields),registers=regs,observe=[f'R{rd}'],
            inspect_pcode=True,
            # Nonzero backing bytes beyond bounds detect an unconditional load.
            memory=[dict(space='ram',address=base+offset,hex=width.to_bytes(4,'little').hex())])
        out.append((CLASS,request,{f'R{rd}':expected,'__native':True}))
    for mode,a,width in itertools.product(('C','W'),[0,1,15,31,32,33,0xffffffff],WIDTHS):
        add(mode,a,width,128,4,0x1234567800,rd=2 if a&1 else 8)
    for mode,units,offset in itertools.product(('C','W'),[0,1,2,3,(1<<(64-bits))-1],
                                             [0,4,12,16,28,32,44,48,65532]):
        for base in [0x1000,0x1234567800,(1<<(bits+shift))-64]:
            add(mode,1,5,units,offset,base)
    for ur in (0,2,4,30,60):add('C',1,3,1,4,0x1000,ur=ur)
    add('C',1,3,0,0,0)
    for pred in (0,1):add('C',1,3,1,4,0x1000,pred=pred)
    return out


def patch(base,mode,units,offset):
    """Pinned CUDA 13.0 SM89 wrapper; refuse a changed register allocation."""
    code=cubin.kernel_image(base,'sem_cx')['code'];start=base.find(code)
    if start<0 or base.find(code,start+1)>=0:raise ValueError('kernel text must be unique')
    decoded={i:pydecode.decode('SM89',code[i:i+16]) for i in range(0,len(code),16)}
    for i in range(0xd0,0x110,16):assert decoded[i].klass.mnemonic=='BAR'
    assert decoded[0x110].klass.name=='bmsk__RRR_RRR'
    assert all(decoded[0x110].env[n]==v for n,v in dict(Rd=0,Ra=2,Rb=5).items())
    assert decoded[0x130].klass.mnemonic=='LOP3'
    assert all(decoded[0x130].env[n]==v for n,v in dict(Rd=7,Ra=0,Rc=7).items())
    assert decoded[0x140].klass.mnemonic=='STG' and decoded[0x140].env['Rb']==7
    assert decoded[0x80].klass.mnemonic=='LDG' and decoded[0x80].env['Rd']==2
    assert decoded[0x30].klass.mnemonic=='ULDC' and decoded[0x30].env['URd']==4
    assert decoded[0x30].env['Sa_bank']==0 and decoded[0x30].env['Sa_addr']==0x118
    assert decoded[0x120].klass.mnemonic=='IMAD' and decoded[0x120].env['Rd']==4
    assert decoded[0x150].klass.mnemonic=='EXIT'
    # The ordinary compiler word supplies scheduling controls. Clear the full
    # five-bit schedule mode before selecting WAIT15_END_GROUP; request all
    # scoreboards. This also drains the preceding input load before the patch.
    control=(int.from_bytes(code[0x110:0x120],'little')&~((1<<105)-1)&~(31<<105))
    control|=(15<<105)|(0x3f<<116)
    replacements=[
        (0xd0,'uldc_const__RCR',dict(URd=0,Sa_bank=0,Sa_addr=0x160,sz='64')),
        (0xe0,'ushf__URuIUR_URIR',dict(URd=0,URa=0,Sb=4,URc=1,dir='R',fmt='U64',hilo='LO')),
        (0xf0,'ushf__URuIUR_URIR',dict(URd=1,URa='URZ',Sb=4,URc=1,dir='R',fmt='U32',hilo='HI')),
        (0x100,'ulop3_lut__URuIUR_URIR',dict(URd=1,URa=1,Sb=units<<13,URc='URZ',imm8=0xfc,UPp='UPT')),
        (0x110,CLASS,dict(Rd=7,Ra=2,URb=0,Sb_offset=offset,cw=mode)),
        (0x130,'nop_',{})]
    image=bytearray(base)
    for address,cls,fields in replacements:
        value=int.from_bytes(bytes.fromhex(encode('SM89',cls,**fields)),'little')
        word=((value&((1<<105)-1))|control).to_bytes(16,'little')
        assert pydecode.decode('SM89',word).klass.name==cls
        image[start+address:start+address+16]=word
    return bytes(image)


def hardware(output,require_gpu=False,compile_only=False,fp='exact'):
    from gpu_semantics import Driver,kernel_request
    output.mkdir(parents=True,exist_ok=True);template=output/'template.cubin'
    subprocess.run([os.environ.get('NVCC','/usr/local/cuda/bin/nvcc'),'--allow-unsupported-compiler',
        '-cubin','-arch=sm_89',str(ROOT/'tests/kernels/bmsk_cx_semantics.cu'),'-o',str(template)],check=True)
    base=template.read_bytes();metadata=cubin.kernel_image(base,'sem_cx')
    assert metadata['parameter_base']==0x160 and metadata['parameters'][0]['offset']==0
    fixtures=[]
    for mode,width in itertools.product(('C','W'),WIDTHS):fixtures.append((mode,128,4,width))
    for units,offset in itertools.product([0,1,2,3,128,0x40000,0x7ffff],[4,16,28,40,64,65532]):
        # Never perform an in-bounds access past the real input allocation.
        if offset>=len(POSITIONS)*12 and offset<units*16:continue
        fixtures.append(('C',units,offset,5))
    images=[patch(base,mode,units,offset) for mode,units,offset,width in fixtures]
    print(f'Validated {len(images)} patched SM89 CX fixtures',flush=True)
    if compile_only:return
    try:driver=Driver()
    except (OSError,RuntimeError) as error:
        if require_gpu:raise
        print('SKIP GPU execution: '+str(error));return
    requests=[];records=[];expected_outputs=[]
    try:
        for index,((mode,units,offset,width),image) in enumerate(zip(fixtures,images)):
            data=b''.join(struct.pack('<III',a,width,0) for a in POSITIONS)
            loaded=struct.unpack_from('<I',data,offset)[0] if offset+4<=units*16 else 0
            expected=[reference(a,loaded,mode) for a in POSITIONS]
            path=output/f'cx_{index}.cubin';path.write_bytes(image)
            actual=list(driver.run(path,'cx',data))
            if actual!=expected:raise AssertionError((mode,units,offset,width,actual[:10],expected[:10]))
            requests.append(kernel_request(image,'cx',data));expected_outputs.append(expected)
            records.append(dict(mode=mode,size=units*16,offset=offset,width=loaded,passed=len(actual)))
        print(f'CX: {sum(r["passed"] for r in records)} GPU/reference comparisons passed',flush=True)
    finally:driver.close()
    language='sm89' if fp=='exact' else 'sm89_readable'
    process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),'SASS:LE:64:'+language],
        input=''.join(json.dumps(r)+'\n' for r in requests),capture_output=True,text=True)
    results=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
    if process.returncode or len(results)!=len(records):raise RuntimeError(process.stderr[-2500:])
    for record,result,expected in zip(records,results,expected_outputs):
        if 'error' in result:raise RuntimeError(result['error'])
        assert 0x110 in result['visited'], 'CX BMSK was not executed'
        actual=[int.from_bytes(bytes.fromhex(v),'little') for v in result['outputs']]
        if actual!=expected:raise AssertionError((record,actual[:10],expected[:10]))
        record.update(steps=result['steps'],visited=result['visited'])
    total=sum(r['passed'] for r in records)
    print(f'CX: {total} whole-kernel {fp} p-code comparisons passed',flush=True)
    (output/f'results_{fp}.json').write_text(json.dumps(dict(architecture='SM89',
        fixture='manually patched SASS in compiler-generated wrapper',fp=fp,
        base_bits=45,base_shift=4,size_shift=4,comparisons=total,results=records),indent=2)+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--arch',default='SM89',choices=ARCHES)
    p.add_argument('--fp',default='exact',choices=('exact','readable'))
    p.add_argument('--require-gpu',action='store_true');p.add_argument('--compile-only',action='store_true')
    p.add_argument('--output',type=Path,default=Path('/tmp/sass-bmsk-cx-oracle'));args=p.parse_args()
    if args.require_gpu or args.compile_only:hardware(args.output,args.require_gpu,args.compile_only,args.fp)
    else:
        from test_semantics import run
        run(args.arch,args.fp,cases(args.arch))


if __name__=='__main__':main()
