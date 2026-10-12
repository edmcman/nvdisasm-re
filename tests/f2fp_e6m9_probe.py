"""Probe SM89 F2FP E6M9 pack selectors against an exact-rational reference.

The wrapper is compiler-generated F32 -> F16 PACK_AB. The probe patches only
its destination-format/rounding/modifier selector bits to request E6M9.
"""
import argparse, bisect, json, random, struct, subprocess, sys
from fractions import Fraction
from pathlib import Path

sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent.parent)]
from sass import cubin, pydecode
from gpu_semantics import Driver, kernel_request
from semantic_cases import encode
from compare_ghidra import GHIDRA_PY, LDEFS, ROOT

OUT=Path('/tmp/sass-f2fp-e6m9-probe')
MAX=0x7dff


def finite_table():
    rows=[]
    for code in range(MAX+1):
        exp=code>>9;mant=code&0x1ff
        value=(Fraction(mant)*Fraction(2)**-39 if exp==0 else
               Fraction(512+mant)*Fraction(2)**(exp-31-9))
        rows.append((value,code))
    return rows


TABLE=finite_table()
VALUES=[x[0] for x in TABLE]


def reference(bits, rnd='RN', sat=False, relu=False):
    from sass.softfloat import unpack
    kind,sign,value=unpack(bits,32)
    signbit=sign<<15
    if kind=='nan':return 0x7fff
    if relu and sign:return 0
    if kind=='inf':return (signbit|MAX) if sat else (signbit|0x7e00)
    value=abs(value)
    if value>VALUES[-1]:
        overflow=VALUES[-1]+Fraction(2)**21
        code=MAX if sat or rnd=='RZ' or value<overflow else 0x7e00
    elif rnd=='RZ':
        code=TABLE[bisect.bisect_right(VALUES,value)-1][1]
    else:
        i=bisect.bisect_left(VALUES,value)
        if i==0:code=0
        elif i==len(VALUES):code=MAX
        else:
            lo,hi=TABLE[i-1],TABLE[i]
            dl=value-lo[0];dh=hi[0]-value
            code=lo[1] if dl<dh or (dl==dh and not (lo[1]&1)) else hi[1]
    if sat and code==0x7e00:code=MAX
    return signbit|code


def input_bits():
    edge=[0,0x80000000,1,0x80000001,0x007fffff,0x00800000,
          0x3f800000,0xbf800000,0x3f000000,0x40000000,
          0x7f7fffff,0xff7fffff,0x7f800000,0xff800000,
          0x7fc00000,0xffc00000,0x7f800001,0x7fffffff]
    f32=lambda value:struct.unpack('<I',struct.pack('<f',value))[0]
    for value in (2.0**-40,2.0**-39,2.0**-30,
                  2.0**32-2.0**22,2.0**32-2.0**21,2.0**32):
        bits=f32(value);edge.extend((bits-1,bits,bits+1))
    rng=random.Random(0xe6a9)
    values=edge+[rng.getrandbits(32) for _ in range(512)]
    while len(values)%32:values.append(rng.getrandbits(32))
    return values


def make_cubins():
    OUT.mkdir(parents=True,exist_ok=True)
    template=OUT/'template.cubin'
    subprocess.run([str(Path('/usr/local/cuda/bin/nvcc')),'--allow-unsupported-compiler',
        '-diag-suppress=177','-cubin','-arch=sm_89',
        str(ROOT/'tests/kernels/scalar_semantics.cu'),'-o',str(template)],check=True)
    base=template.read_bytes()
    words={}
    wrappers=(('f2fp_f16_pack_rn','f2fp__RRR'),
              ('f2fp_f16_pack_imm','f2fp__RIR'),
              ('f2fp_f16_pack_c','f2fp__RCR'))
    for wrapper,expected_class in wrappers:
      meta=cubin.kernel_image(base,'sem_'+wrapper);code=meta['code'];start=base.find(code)
      if start<0 or base.find(code,start+1)>=0:raise RuntimeError(f'{wrapper}: kernel text is not unique')
      found=[]
      for offset in range(0,len(code),16):
        dec=pydecode.decode('SM89',code[offset:offset+16])
        if dec.klass.mnemonic=='F2FP':found.append((offset,dec))
      if len(found)!=1:raise RuntimeError(f'{wrapper}: expected one F2FP, got {len(found)}')
      offset,dec=found[0]
      if dec.klass.name!=expected_class:raise RuntimeError(f'{wrapper}: unexpected form {dec.klass.name}')
      for rnd in ('RN','RZ'):
       for sat in (False,True):
        for relu in (False,True):
         fields={n:v for n,v in dec.env.items() if isinstance(n,str) and n in dec.klass.operand_types}
         fields.update(dstfmt='E6M9',rndMode=rnd,satfinite='SATFINITE' if sat else 'nosatfinite',
                       relu='RELU' if relu else 'norelu')
         inst=int.from_bytes(bytes.fromhex(encode('SM89',dec.klass.name,**fields)),'little')
         old=int.from_bytes(code[offset:offset+16],'little');mask=(1<<105)-1
         image=bytearray(base)
         image[start+offset:start+offset+16]=(inst&mask|old&~mask).to_bytes(16,'little')
         patched=pydecode.decode('SM89',image[start+offset:start+offset+16])
         if patched.klass.mnemonic!='F2FP' or patched.env['dstfmt']!=2:
            raise RuntimeError(f'patched form did not decode: {patched.text}')
         key=f'{wrapper}_{rnd}_{int(sat)}_{int(relu)}';path=OUT/(key+'.cubin')
         path.write_bytes(image);words[key]=(path,wrapper,rnd,sat,relu)
    return words


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--compile-only',action='store_true')
    parser.add_argument('--require-gpu',action='store_true');args=parser.parse_args()
    cubins=make_cubins();print(f'Validated {len(cubins)} patched F2FP E6M9 source/selector forms',flush=True)
    if args.compile_only:return
    vals=input_bits();inputs=[]
    for i,a in enumerate(vals):inputs.extend((a,vals[(i*73+19)%len(vals)],0xdeadbeef))
    data=struct.pack('<%dI'%len(inputs),*inputs)
    try:driver=Driver()
    except (OSError,RuntimeError):
        if args.require_gpu:raise
        raise
    total=0;emulator_requests=[];emulator_expected=[]
    try:
        for name,(path,wrapper,rnd,sat,relu) in cubins.items():
            got=driver.run(str(path),wrapper,data)
            for i,a in enumerate(inputs[::3]):
                b=(inputs[3*i+1] if wrapper=='f2fp_f16_pack_rn' else 0x3fc00000)
                want=(reference(a,rnd,sat,relu)<<16)|reference(b,rnd,sat,relu)
                if got[i]!=want:
                    raise AssertionError((name,i,hex(a),hex(b),hex(got[i]),hex(want)))
                total+=1
            prefix=data[:64*12]
            emulator_requests.append(kernel_request(path.read_bytes(),wrapper,prefix))
            emulator_expected.append(got[:64])
    finally:driver.close()
    process=subprocess.run([GHIDRA_PY,str(ROOT/'tests/ghidra_emulate.py'),str(LDEFS),
        'SASS:LE:64:sm89'],input=''.join(json.dumps(r)+'\n' for r in emulator_requests),
        capture_output=True,text=True)
    results=[json.loads(line) for line in process.stdout.splitlines() if line.startswith('{')]
    if process.returncode or len(results)!=len(emulator_requests):
        raise RuntimeError(process.stderr[-3000:])
    for i,(result,want) in enumerate(zip(results,emulator_expected)):
        if 'error' in result:raise RuntimeError(result['error'])
        if result.get('exit')!='EXIT':raise AssertionError(('kernel did not reach EXIT',i,result))
        got=[int.from_bytes(bytes.fromhex(x),'little') for x in result['outputs']]
        if got!=list(want):raise AssertionError(('p-code/GPU mismatch',i,got[:8],list(want)[:8]))
    pcode_count=len(emulator_expected)*64
    print(f'PASS: {total} E6M9 GPU/reference and {pcode_count} whole-kernel GPU/P-code comparisons')


if __name__=='__main__':main()
