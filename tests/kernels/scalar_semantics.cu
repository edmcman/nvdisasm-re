#include <stdint.h>
#define KERNEL(name, body) extern "C" __global__ void sem_##name(const uint32_t *in, uint32_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r; body; out[i]=r; }
KERNEL(iadd3, r=a+b+c)
KERNEL(imad, asm("mad.lo.u32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(imad_hi, asm("mad.hi.u32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
extern "C" __global__ void sem_imad_hi_c(const uint32_t *in, uint32_t *out, uint64_t addend) {
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x;
 uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r;
 asm volatile("mad.hi.u32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c));
 asm volatile(""::"l"(addend));
 out[i]=r;
}
#define IDP2A(name, instruction) KERNEL(name, asm(instruction " %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
IDP2A(idp2a_lo_ss, "dp2a.lo.s32.s32")
IDP2A(idp2a_lo_su, "dp2a.lo.s32.u32")
IDP2A(idp2a_lo_us, "dp2a.lo.u32.s32")
IDP2A(idp2a_lo_uu, "dp2a.lo.u32.u32")
IDP2A(idp2a_hi_ss, "dp2a.hi.s32.s32")
IDP2A(idp2a_hi_su, "dp2a.hi.s32.u32")
IDP2A(idp2a_hi_us, "dp2a.hi.u32.s32")
IDP2A(idp2a_hi_uu, "dp2a.hi.u32.u32")
#undef IDP2A
KERNEL(lop3, asm("lop3.b32 %0,%1,%2,%3,0x96;":"=r"(r):"r"(a),"r"(b),"r"(c)))
#define VABSDIFF4_FIXTURE(name) KERNEL(name, asm("lop3.b32 %0,%1,%2,%3,0x96;":"=r"(r):"r"(a),"r"(b),"r"(c)))
VABSDIFF4_FIXTURE(vabsdiff4_s8_merge)
VABSDIFF4_FIXTURE(vabsdiff4_u8_merge)
VABSDIFF4_FIXTURE(vabsdiff4_s8_acc)
VABSDIFF4_FIXTURE(vabsdiff4_u8_acc)
#undef VABSDIFF4_FIXTURE
KERNEL(shf, asm("shf.r.wrap.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(c),"r"(b)))
KERNEL(iabs, asm("abs.s32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(imnmx, asm("min.u32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(prmt, asm("prmt.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(brev, asm("brev.b32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(popc, asm("popc.b32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(bfind, asm("bfind.u32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(bfind_shift, asm("bfind.shiftamt.u32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(bfind_s32, asm("bfind.s32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(bfind_shift_s32, asm("bfind.shiftamt.s32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(fadd, asm("add.rn.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmul, asm("mul.rn.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(ffma, asm("fma.rn.f32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(isetp, asm("{ .reg .pred p; setp.lt.u32 p,%1,%2; selp.u32 %0,1,0,p; }":"=r"(r):"r"(a),"r"(b)))
extern "C" __global__ void sem_isetp_ex(const uint64_t *in,uint64_t *out) {
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x;
 uint64_t a=in[3*i],b=in[3*i+1],r;
 asm("{ .reg .pred p; setp.lt.u64 p,%1,%2; selp.u64 %0,1,0,p; }":"=l"(r):"l"(a),"l"(b));
 out[i]=r;
}
KERNEL(sel, asm("{ .reg .pred p; setp.ne.u32 p,%3,0; selp.u32 %0,%1,%2,p; }":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(fsetp, asm("{ .reg .pred p; setp.lt.f32 p,%1,%2; selp.u32 %0,1,0,p; }":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmin_xorsign, { float value; asm("min.xorsign.abs.f32 %0,%1,%2;":"=f"(value):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))); r=__float_as_uint(value); })
KERNEL(fsel, asm("{ .reg .pred p; setp.ne.u32 p,%3,0; neg.f32 %0,%1; selp.f32 %0,%0,%2,p; }":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(fmin, asm("min.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmax, asm("max.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmin_nan, asm("min.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmax_nan, asm("max.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmin_ftz_nan, asm("min.ftz.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmax_ftz_nan, asm("max.ftz.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmin_ftz, asm("min.ftz.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(fmax_ftz, asm("max.ftz.f32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
#define HMNMX2_FIXTURE(name, op) KERNEL(name, asm(op " %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
HMNMX2_FIXTURE(hmnmx2_min, "min.f16x2")
HMNMX2_FIXTURE(hmnmx2_max, "max.f16x2")
HMNMX2_FIXTURE(hmnmx2_xorsign, "min.f16x2")
HMNMX2_FIXTURE(hmnmx2_xorsign_rir, "min.f16x2")
HMNMX2_FIXTURE(hmnmx2_xorsign_rir_noabs, "min.f16x2")
HMNMX2_FIXTURE(hmnmx2_nan_min, "min.f16x2")
HMNMX2_FIXTURE(hmnmx2_nan_max, "max.f16x2")
HMNMX2_FIXTURE(hmnmx2_ftz_min, "min.f16x2")
HMNMX2_FIXTURE(hmnmx2_ftz_max, "max.f16x2")
HMNMX2_FIXTURE(hmnmx2_bf16_min, "min.bf16x2")
HMNMX2_FIXTURE(hmnmx2_bf16_max, "max.bf16x2")
HMNMX2_FIXTURE(hmnmx2_bf16_nan_min, "min.NaN.bf16x2")
HMNMX2_FIXTURE(hmnmx2_bf16_nan_max, "max.NaN.bf16x2")
HMNMX2_FIXTURE(hmnmx2_bf16_xorsign, "min.xorsign.abs.bf16x2")
HMNMX2_FIXTURE(hmnmx2_e6m9_min, "min.bf16x2")
HMNMX2_FIXTURE(hmnmx2_e6m9_max, "max.bf16x2")
HMNMX2_FIXTURE(hmnmx2_e6m9_nan_min, "min.NaN.bf16x2")
HMNMX2_FIXTURE(hmnmx2_e6m9_nan_max, "max.NaN.bf16x2")
HMNMX2_FIXTURE(hmnmx2_e6m9_xorsign, "min.xorsign.abs.bf16x2")
#undef HMNMX2_FIXTURE
KERNEL(f2i, asm("cvt.rzi.s32.f32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(f2i_abs, asm("cvt.rzi.s32.f32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(f2i_neg, asm("cvt.rzi.s32.f32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(f2i_absneg, asm("cvt.rzi.s32.f32 %0,%1;":"=r"(r):"r"(a)))
KERNEL(f2fp_f16_pack_rn, asm("cvt.rn.f16x2.f32 %0,%1,%2;":"=r"(r):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))))
KERNEL(f2fp_f16_pack_rz, asm("cvt.rz.f16x2.f32 %0,%1,%2;":"=r"(r):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))))
KERNEL(f2fp_bf16_pack_rn, asm("cvt.rn.bf16x2.f32 %0,%1,%2;":"=r"(r):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))))
KERNEL(f2fp_bf16_pack_rz, asm("cvt.rz.bf16x2.f32 %0,%1,%2;":"=r"(r):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))))
KERNEL(f2fp_f16_pack_imm, asm("cvt.rn.f16x2.f32 %0,%1,1.5;":"=r"(r):"f"(__uint_as_float(a))))
extern "C" __global__ void sem_f2fp_f16_pack_c(const uint32_t *in,uint32_t *out,float y) {
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x;
 float x=__uint_as_float(in[3*i]);uint32_t r;
 asm("cvt.rn.f16x2.f32 %0,%1,%2;":"=r"(r):"f"(x),"f"(y));
 out[i]=r;
}
#define F2FP_PACK_MOD(name, type, mods) KERNEL(name, asm("cvt.rn" mods "." #type "x2.f32 %0,%1,%2;":"=r"(r):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))))
F2FP_PACK_MOD(f2fp_f16_pack_relu, f16, ".relu")
F2FP_PACK_MOD(f2fp_f16_pack_satfinite, f16, ".satfinite")
F2FP_PACK_MOD(f2fp_f16_pack_relu_satfinite, f16, ".relu.satfinite")
F2FP_PACK_MOD(f2fp_bf16_pack_relu, bf16, ".relu")
F2FP_PACK_MOD(f2fp_bf16_pack_satfinite, bf16, ".satfinite")
F2FP_PACK_MOD(f2fp_bf16_pack_relu_satfinite, bf16, ".relu.satfinite")
#undef F2FP_PACK_MOD
KERNEL(f2fp_e4m3_pack, { uint16_t packed; asm("cvt.rn.satfinite.e4m3x2.f32 %0,%1,%2;":"=h"(packed):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))); r=(c&0xffff0000u)|packed; })
KERNEL(f2fp_e5m2_pack, { uint16_t packed; asm("cvt.rn.satfinite.e5m2x2.f32 %0,%1,%2;":"=h"(packed):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))); r=(c&0xffff0000u)|packed; })
KERNEL(f2fp_e4m3_pack_nosat, { uint16_t packed; asm("cvt.rn.satfinite.e4m3x2.f32 %0,%1,%2;":"=h"(packed):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))); r=(c&0xffff0000u)|packed; })
KERNEL(f2fp_e5m2_pack_nosat, { uint16_t packed; asm("cvt.rn.satfinite.e5m2x2.f32 %0,%1,%2;":"=h"(packed):"f"(__uint_as_float(a)),"f"(__uint_as_float(b))); r=(c&0xffff0000u)|packed; })
#define F2FP_UNPACK(name, fmt) extern "C" __global__ void sem_##name(const uint32_t *in, uint64_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],lo,hi; \
 asm volatile("{ .reg .b16 low,high; mov.b32 {low,high},%2; cvt.rn.f16x2." #fmt "x2 %0,low; cvt.rn.f16x2." #fmt "x2 %1,high; }":"=r"(lo),"=r"(hi):"r"(a)); \
 out[i]=((uint64_t)hi<<32)|lo; }
F2FP_UNPACK(f2fp_e4m3_unpack_h0, e4m3)
F2FP_UNPACK(f2fp_e4m3_unpack_h1, e4m3)
F2FP_UNPACK(f2fp_e5m2_unpack_h0, e5m2)
F2FP_UNPACK(f2fp_e5m2_unpack_h1, e5m2)
F2FP_UNPACK(f2fp_e4m3_unpack_h0_relu, e4m3)
F2FP_UNPACK(f2fp_e4m3_unpack_h1_relu, e4m3)
F2FP_UNPACK(f2fp_e5m2_unpack_h0_relu, e5m2)
F2FP_UNPACK(f2fp_e5m2_unpack_h1_relu, e5m2)
#undef F2FP_UNPACK
KERNEL(f2ip, asm("{ .reg .s32 x,y; cvt.rni.s32.f32 x,%1; cvt.rni.s32.f32 y,%2; cvt.pack.sat.s8.s32.b32 %0,x,y,%3; }":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_s2, asm("cvt.pack.sat.s2.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_u2, asm("cvt.pack.sat.u2.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_s4, asm("cvt.pack.sat.s4.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_u4, asm("cvt.pack.sat.u4.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_s8, asm("cvt.pack.sat.s8.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_u8, asm("cvt.pack.sat.u8.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_s16, asm("cvt.pack.sat.s16.s32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(i2ip_u16, asm("cvt.pack.sat.u16.s32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(i2ip_s8_nosat, asm("cvt.pack.sat.s8.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_s8_satrelu, asm("cvt.pack.sat.s8.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(i2ip_s4_satrelu, asm("cvt.pack.sat.s4.s32.b32 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
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
KERNEL(hmul2, asm("mul.rn.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hmul2_sat, asm("mul.rn.sat.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hmul2_ftz, asm("mul.rn.ftz.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hmul2_fmz, asm("mul.rn.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hadd2_sat, asm("add.rn.sat.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hfma2_sat, asm("fma.rn.sat.f16x2 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(hfma2_move, asm("{ .reg .b32 x,y,z; mov.b32 x,0x80008000; mov.b32 y,0; mov.b32 z,0x3c004000; fma.rn.f16x2 %0,x,y,z; }":"=r"(r)))
// The oracle patches only the arithmetic word of these fixtures to exercise
// SASS lane selectors and modifiers that PTX does not expose directly.
KERNEL(hadd2_lanes, asm("add.rn.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
KERNEL(hfma2_lanes, asm("fma.rn.f16x2 %0,%1,%2,%3;":"=r"(r):"r"(a),"r"(b),"r"(c)))
KERNEL(hmul2_lanes, asm("mul.rn.f16x2 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)))
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
// Conversions over 64-bit inputs and outputs (narrower operands use the low bits).
#define WIDE_KERNEL(name, body) extern "C" __global__ void sem_##name(const uint64_t *in, uint64_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint64_t a=in[3*i],r=0; body; out[i]=r; }
#define CVT(name, op, dt, dc, st, sc) WIDE_KERNEL(name, { dt d; asm(op " %0,%1;":"=" dc(d):sc((st)a)); r=(uint64_t)d; })
#define ROUNDINGS(prefix, kind, op, dt, dc, st, sc) CVT(prefix##_rn, "cvt.rn" kind op, dt, dc, st, sc) \
 CVT(prefix##_rm, "cvt.rm" kind op, dt, dc, st, sc) CVT(prefix##_rp, "cvt.rp" kind op, dt, dc, st, sc) CVT(prefix##_rz, "cvt.rz" kind op, dt, dc, st, sc)
ROUNDINGS(frnd, "i", ".f32.f32", uint32_t, "r", uint32_t, "r")
ROUNDINGS(frnd_f64, "i", ".f64.f64", uint64_t, "l", uint64_t, "l")
ROUNDINGS(f2i_s32, "i", ".s32.f32", uint32_t, "r", uint32_t, "r")
ROUNDINGS(f2i_u32, "i", ".u32.f32", uint32_t, "r", uint32_t, "r")
ROUNDINGS(f2i_s64, "i", ".s64.f32", uint64_t, "l", uint32_t, "r")
ROUNDINGS(f2i_u64, "i", ".u64.f32", uint64_t, "l", uint32_t, "r")
ROUNDINGS(f2i_s32_f64, "i", ".s32.f64", uint32_t, "r", uint64_t, "l")
ROUNDINGS(f2i_u64_f64, "i", ".u64.f64", uint64_t, "l", uint64_t, "l")
CVT(i2f_u32_rm, "cvt.rm.f32.u32", uint32_t, "r", uint32_t, "r") CVT(i2f_u32_rp, "cvt.rp.f32.u32", uint32_t, "r", uint32_t, "r")  // RN/RZ compile to I2FP
ROUNDINGS(i2f_s64, "", ".f32.s64", uint32_t, "r", uint64_t, "l")
ROUNDINGS(i2f_u64, "", ".f32.u64", uint32_t, "r", uint64_t, "l")
ROUNDINGS(i2f_f64_s64, "", ".f64.s64", uint64_t, "l", uint64_t, "l")
CVT(i2f_f64_s32, "cvt.rn.f64.s32", uint64_t, "l", uint32_t, "r")
CVT(i2f_f64_u32, "cvt.rn.f64.u32", uint64_t, "l", uint32_t, "r")
ROUNDINGS(f2f_f32_f64, "", ".f32.f64", uint32_t, "r", uint64_t, "l")
CVT(f2f_f16_f64, "cvt.rn.f16.f64", uint16_t, "h", uint64_t, "l")
CVT(f2f_f64_f32, "cvt.f64.f32", uint64_t, "l", uint32_t, "r")
CVT(f2f_f64_f16, "cvt.f64.f16", uint64_t, "l", uint16_t, "h")
CVT(frnd_ftz_rp, "cvt.rpi.ftz.f32.f32", uint32_t, "r", uint32_t, "r")
CVT(f2i_ftz_rp, "cvt.rpi.ftz.s32.f32", uint32_t, "r", uint32_t, "r")
ROUNDINGS(i2f_f16_s32, "", ".f16.s32", uint16_t, "h", uint32_t, "r")
CVT(i2f_f16_u64, "cvt.rn.f16.u64", uint16_t, "h", uint64_t, "l")
CVT(i2f_s16, "cvt.rn.f32.s16", uint32_t, "r", uint16_t, "h")
CVT(i2f_f64_s16, "cvt.rn.f64.s16", uint64_t, "l", uint16_t, "h")
CVT(f2i_s32_f16_rz, "cvt.rzi.s32.f16", uint32_t, "r", uint16_t, "h")
CVT(f2i_u32_f16_rm, "cvt.rmi.u32.f16", uint32_t, "r", uint16_t, "h")
CVT(f2i_s64_f16_rn, "cvt.rni.s64.f16", uint64_t, "l", uint16_t, "h")
CVT(f2i_u64_f16_rp, "cvt.rpi.u64.f16", uint64_t, "l", uint16_t, "h")
