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
#define DOUBLE_KERNEL(name, body) extern "C" __global__ void sem_##name(const uint64_t *in, uint64_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; double a=__longlong_as_double(in[3*i]),b=__longlong_as_double(in[3*i+1]),c=__longlong_as_double(in[3*i+2]),r; body; out[i]=__double_as_longlong(r); }
DOUBLE_KERNEL(dadd, asm("add.rn.f64 %0,%1,%2;":"=d"(r):"d"(a),"d"(b)))
DOUBLE_KERNEL(dmul, asm("mul.rn.f64 %0,%1,%2;":"=d"(r):"d"(a),"d"(b)))
DOUBLE_KERNEL(dfma, asm("fma.rn.f64 %0,%1,%2,%3;":"=d"(r):"d"(a),"d"(b),"d"(c)))
extern "C" __global__ void sem_dsetp(const uint64_t *in,uint64_t *out) {
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t r;
 double a=__longlong_as_double(in[3*i]),b=__longlong_as_double(in[3*i+1]);
 asm("{ .reg .pred p; setp.ltu.f64 p,%1,%2; selp.u32 %0,1,0,p; }":"=r"(r):"d"(a),"d"(b)); out[i]=r;
}
KERNEL(hadd2, asm("add.rn.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hfma2, asm("fma.rn.f16x2 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(hadd2_sat, asm("add.rn.sat.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hfma2_sat, asm("fma.rn.sat.f16x2 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(hfma2_move, asm("{ .reg .b32 x,y,z; mov.b32 x,0x80008000; mov.b32 y,0; mov.b32 z,0x3c004000; fma.rn.f16x2 %0,x,y,z; }":"=r"(r)))
// The oracle patches only the arithmetic word of these fixtures to exercise
// SASS lane selectors and modifiers that PTX does not expose directly.
KERNEL(hadd2_lanes, asm("add.rn.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hfma2_lanes, asm("fma.rn.f16x2 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
#define HALF_MOVE_FIXTURE(name) KERNEL(name, asm("{ .reg .b32 x,y,z; mov.b32 x,0x80008000; mov.b32 y,0; mov.b32 z,0x3c004000; fma.rn.f16x2 %0,x,y,z; }":"=r"(r)))
HALF_MOVE_FIXTURE(hfma2_move_zero)
HALF_MOVE_FIXTURE(hfma2_move_nan)
HALF_MOVE_FIXTURE(hfma2_move_denorm)
HALF_MOVE_FIXTURE(hfma2_move_inf)
// Directed rounding in every fp32/fp64 arithmetic family.
#define ROUNDED(mode) \
KERNEL(fadd_##mode, asm("add." #mode ".f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b))) \
KERNEL(fmul_##mode, asm("mul." #mode ".f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b))) \
KERNEL(ffma_##mode, asm("fma." #mode ".f32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c))) \
DOUBLE_KERNEL(dadd_##mode, asm("add." #mode ".f64 %0,%1,%2;":"=d"(r):"d"(a),"d"(b))) \
DOUBLE_KERNEL(dmul_##mode, asm("mul." #mode ".f64 %0,%1,%2;":"=d"(r):"d"(a),"d"(b))) \
DOUBLE_KERNEL(dfma_##mode, asm("fma." #mode ".f64 %0,%1,%2,%3;":"=d"(r):"d"(a),"d"(b),"d"(c)))
ROUNDED(rz)
ROUNDED(rm)
ROUNDED(rp)
