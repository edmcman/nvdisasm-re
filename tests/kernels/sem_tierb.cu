#include <stdint.h>
#define KERNEL(name, body) extern "C" __global__ void sem_##name(const uint32_t *in, uint32_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r; body; out[i]=r; }
KERNEL(iadd3, r=a+b+c)
KERNEL(imad, asm("mad.lo.u32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(imad_hi, asm("mad.hi.u32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(lop3, asm("lop3.b32 %0,%1,%2,%3,0x96;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(shf, asm("shf.r.wrap.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(c),"r"(b)))
KERNEL(iabs, asm("abs.s32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(imnmx, asm("min.u32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(prmt, asm("prmt.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(fadd, asm("add.rn.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmul, asm("mul.rn.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(ffma, asm("fma.rn.f32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
