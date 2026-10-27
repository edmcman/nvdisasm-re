"""CUDA backend throughput; run outside the sandbox under nice, with PYTHONPATH=."""
import argparse, ctypes, json, random, statistics, struct, subprocess
from pathlib import Path
from sass.emulate import build
from sass.paths import CACHE, NVCC
from tests.harness import gpu

COUNT, BLOCK, STEPS = 65536, 128, 256
MODES = ('RN', 'RM')
SPECS = [(w, op, mode, 0) for w in (16,32,64) for op in ('add','mul','fma') for mode in MODES]
SPECS += [(32, 'mul', mode, scale) for mode in MODES for scale in (3, 513)]
SPECS += [(64, 'mul', mode, 3) for mode in MODES]
SPECS += [(64, 'mul', mode, scale) for mode in MODES for scale in (1024, 1020)]
FORMATS = {16:(10,15),32:(23,127),64:(52,1023)}

def name(backend, w, op, mode, scale): return f'{backend}_{op}_{w}_{mode}_{scale}'

def source():
 parts=['''#include "sass/fp.hpp"
#ifdef __CUDA_ARCH__
namespace device_native = sass::fp_cuda;
#else
namespace device_native = sass::softfloat;
#endif
''']
 for w,op,mode,scale in SPECS:
  m,_=FORMATS[w]
  keep_nonzero = 1 << (m-1) if scale in (1024, 1020) else 0
  for backend,ns in (('soft','sass::softfloat'), ('cuda','device_native')):
   args='x, b, '+('c, ' if op=='fma' else '')+f'sass::fp_common::{mode}'+(f', {scale}' if op=='mul' else '')
   parts.append(f'''extern "C" __global__ void {name(backend,w,op,mode,scale)}(const uint64_t *in, uint64_t *out) {{
   const int i = blockIdx.x * blockDim.x + threadIdx.x;
   uint64_t x=in[3*i], b=in[3*i+1], c=in[3*i+2], checksum=0;
   const uint64_t exponent = x & ~UINT64_C({(1<<m)-1});
   #pragma unroll 1
   for(int k=0; k<{STEPS}; ++k) {{
     const uint64_t r={ns}::{op}<{w}>({args});
     checksum+=r;
     x=(r & UINT64_C({(1<<m)-1})) | exponent | UINT64_C({keep_nonzero});
   }}
   out[i] = checksum ^ x;
 }}''')
 return '\n'.join(parts)

def main():
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--output', type=Path, default=CACHE / 'fp-benchmark')
 output=parser.parse_args().output
 output.mkdir(parents=True, exist_ok=True)
 driver=gpu.Driver()
 print(driver.device, driver.arch, flush=True)
 cpp=output / 'kernels.cu'; cpp.write_text(source())
 cubin=output / 'kernels.cubin'
 subprocess.run([NVCC,'--allow-unsupported-compiler','-std=c++20','-ftz=false','--fmad=false',
                 '-diag-suppress=177','-DSASS_FP_SOFT','-I',str(build.INCLUDE),'-cubin',f'-arch=sm_{driver.arch[2:]}',
                 str(cpp),'-o',str(cubin)],check=True)
 module=ctypes.c_void_p(); start=ctypes.c_void_p(); end=ctypes.c_void_p()
 driver.call('cuModuleLoadData',ctypes.byref(module),ctypes.c_char_p(cubin.read_bytes()))
 driver.call('cuEventCreate',ctypes.byref(start),0); driver.call('cuEventCreate',ctypes.byref(end),0)
 rows=[]
 try:
  rng=random.Random(95)
  for w in (16,32,64):
   m,bias=FORMATS[w]
   values=[]
   for i in range(COUNT):
    values += [(rng.randrange(bias-2,bias+3)<<m)|rng.getrandbits(m),
               (bias<<m)|rng.getrandbits(m),
               (rng.randrange(bias-2,bias+3)<<m)|rng.getrandbits(m)]
   data=struct.pack('<'+str(len(values))+'Q',*values)
   with driver.arena(data) as input_ptr, driver.arena(bytes(COUNT*8)) as output_ptr:
    for width,op,mode,scale in [spec for spec in SPECS if spec[0]==w]:
     if op=='mul' and scale==3:
      # Preserve ordinary finite products in the scaled-multiply path.
      scaled_values=values.copy()
      for i in range(COUNT): scaled_values[3*i+1] -= 3<<m
      actual_data=struct.pack('<'+str(len(values))+'Q',*scaled_values)
     elif op=='mul' and scale in (1024, 1020):
      # Two subnormal operands force exponent redistribution. Scale 1024 gives normal
      # outputs, scale 1020 gives subnormal outputs, keeping the significands identical.
      scaled_values=values.copy()
      for i in range(COUNT):
       scaled_values[3*i] = (values[3*i] & ((1<<m)-1)) | (1<<(m-1))
       scaled_values[3*i+1] = (values[3*i+1] & ((1<<m)-1)) | (1<<(m-1))
      actual_data=struct.pack('<'+str(len(values))+'Q',*scaled_values)
     else: actual_data=data
     driver.call('cuMemcpyHtoD_v2',input_ptr,ctypes.create_string_buffer(actual_data),ctypes.c_size_t(len(actual_data)))
     params=(ctypes.c_void_p*2)(ctypes.cast(ctypes.byref(input_ptr),ctypes.c_void_p), ctypes.cast(ctypes.byref(output_ptr),ctypes.c_void_p))
     functions={}
     for backend in ('soft','cuda'):
      fn=ctypes.c_void_p(); driver.call('cuModuleGetFunction',ctypes.byref(fn),module,name(backend,w,op,mode,scale).encode()); functions[backend]=fn
     def launch(fn):
      driver.call('cuLaunchKernel',fn,ctypes.c_uint(COUNT//BLOCK),ctypes.c_uint(1),ctypes.c_uint(1),ctypes.c_uint(BLOCK),ctypes.c_uint(1),ctypes.c_uint(1),
                  ctypes.c_uint(0),ctypes.c_void_p(),params,ctypes.c_void_p())
     for _ in range(2):
      for backend in ('soft','cuda'): launch(functions[backend])
     driver.call('cuCtxSynchronize')
     timings={'soft':[], 'cuda':[]}; outputs={}
     for repeat in range(7):
      for backend in (('soft','cuda') if repeat%2==0 else ('cuda','soft')):
       driver.call('cuEventRecord',start,ctypes.c_void_p()); launch(functions[backend]); driver.call('cuEventRecord',end,ctypes.c_void_p())
       driver.call('cuEventSynchronize',end)
       elapsed=ctypes.c_float(); driver.call('cuEventElapsedTime',ctypes.byref(elapsed),start,end); timings[backend].append(elapsed.value)
       if repeat==6:
        result=ctypes.create_string_buffer(COUNT*8); driver.call('cuMemcpyDtoH_v2',result,output_ptr,ctypes.c_size_t(len(result))); outputs[backend]=result.raw
     assert outputs['soft']==outputs['cuda'], (w,op,mode,scale)
     soft,cuda=(statistics.median(timings[b]) for b in ('soft','cuda'))
     row=dict(width=w,op=op,mode=mode,scale=scale,soft_ms=soft,cuda_ms=cuda,speedup=soft/cuda,cuda_gops=COUNT*STEPS/(cuda*1e6))
     rows.append(row)
     print(f'{w} {op} {mode} scale={scale}: soft={soft:.4f} ms cuda={cuda:.4f} ms speedup={soft/cuda:.2f}x throughput={COUNT*STEPS/(cuda*1e6):.2f} Gop/s',flush=True)
 finally:
  driver.call('cuEventDestroy_v2',start); driver.call('cuEventDestroy_v2',end); driver.call('cuModuleUnload',module); driver.close()
 (output / 'results.json').write_text(json.dumps(dict(count=COUNT,steps=STEPS,device=driver.device,arch=driver.arch,results=rows),indent=2))

if __name__=='__main__': main()
