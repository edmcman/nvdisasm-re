"""Hardware-established SASS carry conventions and whole-kernel p-code checks.

Reserve eight instruction slots in a compiled CUDA kernel, then replace them
with predicate initialization, one target instruction, and carry readback. CUDA
launch metadata, prologue, global memory operations and EXIT remain compiled.
"""
import argparse,itertools,json,os,random,subprocess,sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from gpu_semantics import Driver,kernel_request
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT
from semantic_cases import encode
from sass import cubin,pydecode
def reference(cls,fields,observe,vector):
    a,b,c=vector;mask=0xffffffff;mask64=(1<<64)-1;extended='_x_' in cls
    p=(c&1)^fields.get('Pp@not',0) if extended else 0
    q=((c>>1)&1)^fields.get('Pq@not',0) if extended else 0
    if cls.startswith('iadd3'):
        inputs=[a,b,c]
        for i,name in enumerate(('Ra','Rb','Rc')):
            if fields.get(name+('@invert' if extended else '@negate')):inputs[i]=mask-inputs[i]+(0 if extended else 1)
        total=sum(inputs)+p+q
        if observe=='pu':return int(total>mask)
        return total&mask if observe=='result' else int(total>mask)+2*int(total>0x1ffffffff)
    if cls.startswith('lea'):
        high='_hi_' in cls
        source=((a-(1<<32) if a>>31 else a) if '_sx32' in cls else (c<<32)|a) if high else a
        word=((source<<fields['scaleU5'])>>(32 if high else 0))&mask
        if fields.get('Ra@invert' if extended else 'Ra@negate'):word=mask-word+(0 if extended else 1)
        if fields.get('Rb@invert' if extended else 'Rb@negate'):b=mask-b+(0 if extended else 1)
        total=word+b+p
        return total&mask if observe=='result' else int(total>mask)
    x=a-(1<<32) if fields['fmt']=='S32' and a>>31 else a
    y=b-(1<<32) if fields['fmt']=='S32' and b>>31 else b
    product=(x*y)&mask64;wide='_hi_' in cls or '_wide_' in cls
    addend=(b<<32)|c if wide else c
    if fields.get('Rc@invert'):addend^=mask64 if wide else mask
    total=product+addend+p
    if observe=='pu':return int(total>mask64)
    if observe=='high' or '_hi_' in cls:return (total>>32)&mask
    return total&mask


def main():
    p=argparse.ArgumentParser();p.add_argument('--require-gpu',action='store_true');p.add_argument('--compile-only',action='store_true')
    p.add_argument('--output',type=Path,default=Path('/tmp/sass-carry-oracle'));args=p.parse_args()
    outdir=args.output;outdir.mkdir(parents=True,exist_ok=True);cubin_path=outdir/'template.cubin'
    subprocess.run([os.environ.get('NVCC','/usr/local/cuda/bin/nvcc'),'--allow-unsupported-compiler','-cubin','-arch=sm_89',
                    str(ROOT/'tests/kernels/carry_semantics.cu'),'-o',str(cubin_path)],check=True)
    base=cubin_path.read_bytes();code=cubin.kernel_image(base,'sem_probe')['code'];start=base.find(code)
    if start<0 or base.find(code,start+1)>=0:raise RuntimeError('probe text is not unique in cubin')
    decoded=[pydecode.decode('SM89',code[i:i+16]) for i in range(0,len(code),16)]
    bars=[i*16 for i,d in enumerate(decoded) if d.klass.mnemonic=='BAR']
    if len(bars)!=8 or bars!=list(range(0x100,0x180,16)):raise RuntimeError('compiler changed reserved probe slots')
    mad=decoded[0x180//16]
    if mad.klass.mnemonic!='IMAD' or any(mad.env[n]!=v for n,v in dict(Ra=2,Rb=5,Rc=6,Rd=11).items()):
        raise RuntimeError('compiler changed probe register assignment')
    rng=random.Random(44);edges=[0,1,2,3,0xffffffff,0xfffffffe,0x7fffffff,0x80000000,0x80000001,0xffff0000]
    vectors=[(a,b,edges[(i*7+4)%len(edges)]) for i,(a,b) in enumerate(itertools.product(edges,repeat=2))]
    vectors += [(0xffffffff,0xffffffff,0xffffffff),(0xffffffff,0xffffffff,3),(0x80000000,0x80000000,0)]
    vectors += [tuple(rng.getrandbits(32) for _ in range(3)) for i in range(25)]
    import struct
    data=b''.join(struct.pack('<III',*v) for v in vectors)
    if args.compile_only:print('Compiled and confirmed carry probe slots');return
    try:driver=Driver()
    except (OSError,RuntimeError) as error:
        if args.require_gpu:raise
        print('SKIP GPU execution: '+str(error));return
    records=[];requests=[]
    def word(cls,**fields):return bytes.fromhex(encode('SM89',cls,**fields))
    prep=[word('lop3_lut__RuIR_RIR',Rd=0,Ra=6,Sb=1,Rc=255,imm8=0xc0,Pp=7),
          word('isetp__RsIR_RIR_noEX',Pu=2,Pv=7,Ra=0,Sb=0,icmp='NE',bop='AND',Pp=7,fmt='U32'),
          word('lop3_lut__RuIR_RIR',Rd=0,Ra=6,Sb=2,Rc=255,imm8=0xc0,Pp=7),
          word('isetp__RsIR_RIR_noEX',Pu=3,Pv=7,Ra=0,Sb=0,icmp='NE',bop='AND',Pp=7,fmt='U32')]
    # Copy the original instruction's dependency waits and stalls to each probe
    # word; wait for every load in the first word, retaining normal prologue code.
    control=int.from_bytes(code[0x180:0x190],'little')&~((1<<105)-1)
    controlfirst=(control&~(0x3f<<116))|(0x3f<<116)
    def run(cls,fields,observe='result'):
        op=word(cls,**fields)
        suffix=[word('nop_')]*3
        if observe=='carry':
            suffix=[word('sel__RuIR_RIR',Rd=0,Ra=255,Sb=1,Pp=0,**{'Pp@not':1}),
              word('sel__RuIR_RIR',Rd=11,Ra=255,Sb=2,Pp=1,**{'Pp@not':1}),
              word('iadd3_noimm__RRR_RRR',Rd=11,Ra=0,Rb=11,Rc=255)]
        elif observe in ('low','high'):
            suffix[-1]=word('mov__RR',Rd=11,Rb=10 if observe=='low' else 11,PixMaskU04=15)
        elif observe=='pu':suffix[-1]=word('sel__RuIR_RIR',Rd=11,Ra=255,Sb=1,Pp=0,**{'Pp@not':1})
        seq=prep+[op]+suffix
        image=bytearray(base)
        # IMAD pair addend C is low R6, high R7. The high word is the ordinary B input.
        if cls.startswith('imad_') and fields.get('wide')!='LO' and ('_hi_' in cls or '_wide_' in cls):
            seq[2]=word('mov__RR',Rd=7,Rb=5,PixMaskU04=15) # Pq is unused by IMAD
            seq[3]=word('nop_')
        for i,w in enumerate(seq):
            encoded=int.from_bytes(w,'little')&((1<<105)-1)
            image[start+0x100+16*i:start+0x110+16*i]=(encoded|(controlfirst if i==0 else control)).to_bytes(16,'little')
        image[start+0x180:start+0x190]=(int.from_bytes(word('nop_'),'little')&((1<<105)-1)|control).to_bytes(16,'little')
        path=outdir/'probe.cubin';path.write_bytes(image)
        result=driver.run(path,'probe',data)
        return result,bytes(image)
    def check(cls,fields,observe):
        outputs,image=run(cls,fields,observe)
        for i,(vector,got) in enumerate(zip(vectors,outputs)):
            want=reference(cls,fields,observe,vector)
            if got!=want:raise AssertionError((cls,fields,observe,i,vector,hex(got),hex(want)))
        records.append(dict(cls=cls,fields=fields,observe=observe,outputs=outputs))
        requests.append(kernel_request(image,'probe',data))
    try:
        for extended in (False,True):
            cls='iadd3_x_noimm__RRR_RRR' if extended else 'iadd3_noimm__RRR_RRR'
            for flags in (0,1,2,4,5,6):
                fields=dict(Rd=11,Pu=0,Pv=1,Ra=2,Rb=5,Rc=6)
                if extended:fields.update(Pp=2,Pq=3)
                fields.update({n+'@'+('invert' if extended else 'negate'):(flags>>i)&1 for i,n in enumerate(['Ra','Rb','Rc'])})
                for observe in ('result','carry'):check(cls,fields,observe)
            check(cls,dict(Rd=11,Pu=0,Pv=0,Ra=2,Rb=5,Rc=6,**(dict(Pp=2,Pq=3) if extended else {})),'pu')
        for cls in ['lea_lo_noimm__RRR_RRR','lea_lo_noimm_x__RRR_RRR','lea_hi_noimm__RRR_RRR','lea_hi_noimm_x__RRR_RRR','lea_hi_noimm_sx32__RRR_RRR','lea_hi_noimm_sx32_x__RRR_RRR']:
            for shift in (0,1,5,31):
                for flags in (0,1,2):
                    fields=dict(Rd=11,Pu=0,Ra=2,Rb=5,scaleU5=shift)
                    if '_hi_' in cls and '_sx32' not in cls:fields['Rc']=6
                    if '_x__' in cls:fields['Pp']=2
                    fields.update({n+'@'+('invert' if '_x__' in cls else 'negate'):(flags>>i)&1 for i,n in enumerate(['Ra','Rb'])})
                    for observe in ('result','pu'):check(cls,fields,observe)
        for cls in ['imad_x__RRR_RRR','imad_hi_x__RRR_RRR','imad_wide_x__RRR_RRR','imad_hi__RRR_RRR','imad_wide__RRR_RRR']:
            for fmt in ('U32','S32'):
                for invert in (0,1):
                    if '_x__' not in cls and invert:continue
                    fields=dict(Rd=10 if '_wide_' in cls else 11,Ra=2,Rb=5,Rc=6,fmt=fmt)
                    if '_hi_' in cls or '_wide_' in cls:fields['Pu']=0
                    if '_x__' in cls:fields.update(Pp=2,**{'Rc@invert':invert})
                    for observe in (('low','high','pu') if '_wide_' in cls else ('result','pu') if '_hi_' in cls else ('result',)):
                        check(cls,fields,observe)
        for invert in (1,2,3):
            fields=dict(Rd=11,Pu=0,Pv=1,Ra=2,Rb=5,Rc=6,Pp=2,Pq=3,**{'Pp@not':invert&1,'Pq@not':invert>>1})
            for observe in ('result','carry'):check('iadd3_x_noimm__RRR_RRR',fields,observe)
        print(f'GPU arithmetic references passed for {len(records)} variants',flush=True)
        process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),'SASS:LE:64:sm89'],
            input=''.join(json.dumps(r)+'\n' for r in requests),capture_output=True,text=True)
        emulated=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
        if len(emulated)!=len(records):raise RuntimeError(process.stderr[-2000:])
        for record,result in zip(records,emulated):
            if 'error' in result:raise RuntimeError((record['cls'],record['fields'],result['error']))
            if result.get('exit')!='EXIT' or 0x140 not in result.get('visited',[]):
                raise AssertionError((record['cls'],'target instruction did not execute through EXIT'))
            actual=[int.from_bytes(bytes.fromhex(value),'little') for value in result['outputs']]
            if len(actual)!=len(vectors) or list(record['outputs'])!=actual:raise AssertionError((record['cls'],record['fields'],record['observe'],actual))
        total=len(records)*len(vectors)
        (outdir/'results.json').write_text(json.dumps(dict(architecture='SM89',mode='patched whole-kernel',seed=44,
            threads_per_variant=len(vectors),variants=len(records),passed=total,records=records,
            note='eight BAR slots and the placeholder arithmetic word replaced; compiled prologue/addressing/EXIT preserved'),indent=2)+'\n')
        print(f'SM89 carry chains: {total} GPU/reference/p-code comparisons passed',flush=True)
    finally:driver.close()

if __name__=='__main__':main()
