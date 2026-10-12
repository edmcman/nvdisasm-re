"""Probe SM89 F2FP's CX source with patched SASS, GPU, and whole-kernel P-code.

The wrapper shares the validated uniform descriptor construction from the BMSK
CX probe. Only its target instruction is replaced with F2FP.F16.PACK_AB.
"""
import argparse,json,random,struct,subprocess
from pathlib import Path
from bmsk_cx_semantics import patch as patch_template
from compare_ghidra import GHIDRA_PY,LDEFS,ROOT
from sass import cubin,pydecode
from sass import softfloat
from semantic_cases import encode
from gpu_semantics import Driver,kernel_request

CLASS='f2fp__RCxR'
OUT=Path('/tmp/sass-f2fp-cx-probe')


def patched_image(base,units=128,offset=4):
    """Reuse the proven CX header prologue and patch BMSK to F2FP."""
    image=bytearray(patch_template(base,'C',units,offset))
    code=cubin.kernel_image(base,'sem_cx')['code'];start=base.find(code)
    control=int.from_bytes(image[start+0x110:start+0x120],'little')&~((1<<105)-1)
    fields=dict(Rd=7,Ra=2,URb=0,Sb_offset=offset,dstfmt='F16',srcfmt='F32',
                merge='PACK_AB',rndMode='RN',relu='norelu',satfinite='nosatfinite')
    word=int.from_bytes(bytes.fromhex(encode('SM89',CLASS,**fields)),'little')
    word=((word&((1<<105)-1))|control).to_bytes(16,'little')
    target=pydecode.decode('SM89',word)
    if target.klass.name!=CLASS:raise RuntimeError(f'patched word decoded as {target.text}')
    image[start+0x110:start+0x120]=word
    return bytes(image)


def half(bits):
    kind,sign,value=softfloat.unpack(bits,32)
    if kind=='nan':return 0x7e00
    if kind=='inf':return (sign<<15)|0x7c00
    return softfloat.pack(value,16,'RN',sign)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--compile-only',action='store_true')
    parser.add_argument('--require-gpu',action='store_true')
    parser.add_argument('--output',type=Path,default=OUT)
    parser.add_argument('--fp',choices=('exact','readable'),default='exact')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    template=args.output/'template.cubin'
    subprocess.run(['/usr/local/cuda/bin/nvcc','--allow-unsupported-compiler',
        '-cubin','-arch=sm_89',str(ROOT/'tests/kernels/bmsk_cx_semantics.cu'),
        '-o',str(template)],check=True)
    base=template.read_bytes();meta=cubin.kernel_image(base,'sem_cx')
    if meta['parameter_base']!=0x160 or [p['size'] for p in meta['parameters']]!=[8,8]:
        raise RuntimeError('unexpected CX wrapper parameter ABI')
    image=patched_image(base)
    print('Validated patched F2FP.F16.PACK_AB CX instruction',flush=True)
    if args.compile_only:return

    rng=random.Random(0xf2f0)
    vals=[0,0x80000000,0x3f800000,0xbf800000,0x3fc00000,0xc0200000,
          0x387fc000,0x38800000,0x477fe000,0x477ff000,0x33000000,0xb3800000]
    vals += [int.from_bytes(struct.pack('<f',rng.uniform(-500.0,500.0)),'little')
             for _ in range(116)]
    data=b''.join(struct.pack('<III',a,b,0) for a,b in zip(vals,vals[1:]+vals[:1]))
    path=args.output/'f2fp_cx.cubin';path.write_bytes(image)
    # The CX descriptor base is warp-uniform, so every lane reads the same
    # source B from the first record at the encoded byte offset.
    cx_value=vals[1]
    expected=[(half(a)<<16)|half(cx_value) for a in vals]
    driver=Driver()
    try:
        actual=list(driver.run(str(path),'cx',data))
    finally:driver.close()
    if actual!=expected:
        mismatch=next(i for i,(a,b) in enumerate(zip(actual,expected)) if a!=b)
        raise AssertionError(('GPU/reference mismatch',mismatch,hex(actual[mismatch]),hex(expected[mismatch])))
    request=kernel_request(image,'cx',data)
    language='sm89' if args.fp=='exact' else 'sm89_readable'
    process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),
        'SASS:LE:64:'+language],input=json.dumps(request)+'\n',capture_output=True,text=True)
    results=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
    if process.returncode or len(results)!=1:raise RuntimeError(process.stderr[-2000:])
    result=results[0]
    if 'error' in result:raise RuntimeError(result['error'])
    if 0x110 not in result['visited']:raise AssertionError('F2FP CX instruction was not executed')
    emulated=[int.from_bytes(bytes.fromhex(x),'little') for x in result['outputs']]
    if emulated!=expected:raise AssertionError(('P-code/reference mismatch',emulated[:8],expected[:8]))
    print(f'PASS: {len(expected)} F2FP CX GPU/reference and {len(expected)} whole-kernel {args.fp} P-code comparisons')
    (args.output/f'results_{args.fp}.json').write_text(json.dumps({
        'architecture':'SM89','class':CLASS,'selectors':['F16','F32','PACK_AB','RN'],
        'source_forms':['register A','CX B at c[0][0x160+4]'],
        'comparisons':len(expected),'pcode_mode':args.fp,'result':'pass'},indent=2)+'\n')


if __name__=='__main__':main()
