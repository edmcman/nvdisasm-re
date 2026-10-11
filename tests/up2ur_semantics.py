"""Patched SM89 UP2UR GPU/reference/p-code oracle."""
import argparse,json,os,struct,subprocess,sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from carry_semantics import Driver
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT
from gpu_semantics import kernel_request
from sass import cubin,pydecode
from semantic_cases import encode


def word(cls,**fields):
    return int.from_bytes(bytes.fromhex(encode('SM89',cls,**fields)),'little')


def main():
    p=argparse.ArgumentParser();p.add_argument('--require-gpu',action='store_true')
    p.add_argument('--output',type=Path,default=Path('/tmp/sass-up2ur-oracle'))
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    template=args.output/'template.cubin'
    subprocess.run([os.environ.get('NVCC','/usr/local/cuda-13.0/bin/nvcc'),
        '--allow-unsupported-compiler','-cubin','-arch=sm_89',
        '-I/usr/local/cuda-13.0/targets/x86_64-linux/include',
        str(ROOT/'tests/kernels/carry_semantics.cu'),'-o',str(template)],check=True)
    base=template.read_bytes();code=cubin.kernel_image(base,'sem_probe')['code'];start=base.find(code)
    if start<0 or base.find(code,start+1)>=0:raise RuntimeError('kernel text is not unique')
    decoded=[pydecode.decode('SM89',code[i:i+16]) for i in range(0,len(code),16)]
    bars=[i*16 for i,d in enumerate(decoded) if d.klass.mnemonic=='BAR']
    if bars!=list(range(0x100,0x180,16)):
        raise RuntimeError(f'compiler changed reserved slots: {bars}')
    placeholder=decoded[0x180//16]
    if placeholder.klass.mnemonic!='IMAD' or any(placeholder.env[n]!=v for n,v in
            dict(Ra=2,Rb=5,Rc=6,Rd=11).items()):
        raise RuntimeError('compiler changed placeholder register assignment')
    data=b''.join(struct.pack('<III',i,0,0) for i in range(128))
    pred_bits=0b1011010
    control=int.from_bytes(code[0x180:0x190],'little')&~((1<<105)-1)
    controlfirst=(control&~(0x3f<<116))|(0x3f<<116)
    requests=[];paths=[];expected=[]
    masks=(0,1,2,0x55,0x7f,0xff,0x24,0x80000024,0xffffffff)
    for mask in masks:
        selected=[bit for bit in range(7) if mask&(1<<bit)]
        remaining=list(selected);prep=[word('umov__UI',URd=4,Sb=0x100)]
        while remaining:
            bit=remaining.pop(0);value=bool(pred_bits&(1<<bit))
            opposite=next((other for other in remaining
                           if bool(pred_bits&(1<<other))!=value),None)
            upv=opposite if opposite is not None else 'UPT'
            if opposite is not None:remaining.remove(opposite)
            prep.append(word('uisetp__URURUR_URURUR',UPu=bit,UPv=upv,
                URa='URZ',URb='URZ',UPp='UPT',icmp='EQ' if value else 'NE',
                fmt='U32',bop='AND'))
        if len(prep)>6:raise RuntimeError(f'mask {mask:#x} needs too many UISETPs')
        prep += [word('nop_') for _ in range(6-len(prep))]
        sequence=prep+[word('up2ur__Imm',URd=5,UPR='UPR',URa=4,Sb=mask,insert='B0'),word('nop_')]
        image=bytearray(base)
        for i,value in enumerate(sequence):
            sched=controlfirst if i==0 else control
            offset=0x100+16*i
            image[start+offset:start+offset+16]=(value&((1<<105)-1)|sched).to_bytes(16,'little')
        move=word('mov__RU',Rd=11,URb=5,PixMaskU04=15)
        image[start+0x180:start+0x190]=(move&((1<<105)-1)|control).to_bytes(16,'little')
        path=args.output/f'up2ur_mask_{mask:02x}.cubin';path.write_bytes(image);paths.append(path)
        want=0x100|(pred_bits&mask&0x7f)
        expected.append(want)
        requests.append(kernel_request(image,'probe',data))
    try:driver=Driver()
    except (OSError,RuntimeError) as error:
        if args.require_gpu:raise
        print('SKIP GPU execution: '+str(error));return
    try:
        for mask,path,want in zip(masks,paths,expected):
            got=driver.run(path,'probe',data)
            print(f'mask={mask:#x} GPU={got[0]:#x} expected={want:#x}',flush=True)
            if got!=(want,)*128:
                raise AssertionError(f'mask {mask:#x}: GPU got {got[:8]}, want {want:#x}')
    finally:driver.close()
    process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),
        'SASS:LE:64:sm89'],input=''.join(json.dumps(r)+'\n' for r in requests),
        capture_output=True,text=True)
    results=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
    if len(results)!=len(requests):raise RuntimeError(process.stderr[-2500:])
    for mask,want,result in zip(masks,expected,results):
        if 'error' in result:raise RuntimeError((mask,result['error']))
        if result.get('exit')!='EXIT' or 0x160 not in result.get('visited',[]):
            raise AssertionError((mask,'UP2UR was not executed through EXIT'))
        outputs=[int.from_bytes(bytes.fromhex(x),'little') for x in result['outputs']]
        if outputs!=[want]*128:raise AssertionError((mask,outputs[:8],want))
    (args.output/'results.json').write_text(json.dumps(dict(architecture='SM89',
        fixture='manually patched SASS in compiled whole-kernel wrapper',
        predicate_bits=pred_bits,masks=list(masks),threads_per_mask=128,
        comparisons=len(masks)*128,passed=True,
        rule='URd = URa | sum(UPi << i for selected mask bits i=0..6); bits above 6 ignored'),indent=2)+'\n')
    print(f'SM89 UP2UR: {len(masks)*128} GPU/reference/p-code comparisons passed')


if __name__=='__main__':main()
