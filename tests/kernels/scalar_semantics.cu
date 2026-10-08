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
KERNEL(isetp, asm("{ .reg .pred p; setp.lt.u32 p,%1,%2; selp.u32 %0,1,0,p; }":"=r"(r):"r"(a),"r"(b)))
KERNEL(sel, asm("{ .reg .pred p; setp.ne.u32 p,%3,0; selp.u32 %0,%1,%2,p; }":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(fsetp, asm("{ .reg .pred p; setp.lt.f32 p,%1,%2; selp.u32 %0,1,0,p; }":"=r"(r):"r"(a),"r"(b)))
KERNEL(fsel, asm("{ .reg .pred p; setp.ne.u32 p,%3,0; neg.f32 %0,%1; selp.f32 %0,%0,%2,p; }":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(fmin, asm("min.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmax, asm("max.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmin_ftz, asm("min.ftz.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmax_ftz, asm("max.ftz.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(f2i, asm("cvt.rzi.s32.f32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(i2f, asm("cvt.rn.f32.s32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(fadd_ftz, asm("add.rn.ftz.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmul_rz, asm("mul.rz.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(ffma_sat, asm("fma.rn.sat.f32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(shared, __shared__ uint32_t cell[32]; unsigned offset=threadIdx.x; unsigned addr=__cvta_generic_to_shared(&cell[offset]); asm volatile("st.volatile.shared.u32 [%1],%2; ld.volatile.shared.u32 %0,[%1];":"=r"(r):"r"(addr),"r"(a):"memory"))
KERNEL(local, volatile uint32_t cell[256]; for (unsigned j=0;j<256;++j) cell[j]=a+j*b; r=cell[c&255])
extern "C" __global__ void sem_branch(const uint32_t *in,uint32_t *out) {
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x;
 uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r=a;
 #pragma unroll 1
 for (unsigned j=0;j<(b&7);++j) { asm volatile("":::"memory");r=r*3+c; }
 out[i]=r;
}
__constant__ uint32_t lookup[32]={0,1,4,9,16,25,36,49,64,81,100,121,144,169,196,225,
 256,289,324,361,400,441,484,529,576,625,676,729,784,841,900,961};
KERNEL(ldc, r=lookup[a&31])
