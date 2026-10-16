#include <stdint.h>
// One atomic per thread on its own cell: the cell starts as a, the atomic applies b (CAS: compare b,
// new c), and the output is returned | final << 32 (64-bit atomics: final + returned * K, F64: final).
// Global cells are the thread's output slot, shared ones its own word; generic ATOM reaches the
// global cell or (c odd) the shared one.
#define K 0x9e3779b97f4a7c15ull
#define GLOBAL32(name, ptx, ...) extern "C" __global__ void atom_##name(const uint32_t *in, uint64_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r=a; \
 uint32_t *cell=(uint32_t *)(out+i)+1; *(volatile uint32_t *)cell=a; \
 asm volatile(ptx:__VA_ARGS__:"memory"); (void)c; \
 out[i]=(uint64_t)*(volatile uint32_t *)cell<<32|r; }
#define SHARED32(name, ptx, ...) extern "C" __global__ void atom_##name(const uint32_t *in, uint64_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r=a; \
 __shared__ uint32_t cells[1024]; uint32_t *cell=&cells[threadIdx.x]; *(volatile uint32_t *)cell=a; \
 unsigned s=(unsigned)__cvta_generic_to_shared(cell); asm volatile(ptx:__VA_ARGS__:"memory"); (void)c; \
 out[i]=(uint64_t)*(volatile uint32_t *)cell<<32|r; }
#define GLOBAL64(name, ptx, ...) extern "C" __global__ void atom_##name(const uint64_t *in, uint64_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint64_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r=a; \
 uint64_t *cell=out+i; *(volatile uint64_t *)cell=a; \
 asm volatile(ptx:__VA_ARGS__:"memory"); (void)c; \
 out[i]=*(volatile uint64_t *)cell+r*K; }

GLOBAL32(add_u32, "atom.global.add.u32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(min_s32, "atom.global.min.s32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(min_u32, "atom.global.min.u32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(max_s32, "atom.global.max.s32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(max_u32, "atom.global.max.u32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(inc_u32, "atom.global.inc.u32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(dec_u32, "atom.global.dec.u32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(and_b32, "atom.global.and.b32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(or_b32, "atom.global.or.b32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(xor_b32, "atom.global.xor.b32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(exch_b32, "atom.global.exch.b32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(cas_b32, "atom.global.cas.b32 %0,[%1],%2,%3;", "=r"(r):"l"(cell),"r"(b),"r"(c))
GLOBAL32(add_f32, "atom.global.add.f32 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(add_f16x2, "atom.global.add.noftz.f16x2 %0,[%1],%2;", "=r"(r):"l"(cell),"r"(b))
GLOBAL32(red_add_u32, "red.global.add.u32 [%0],%1;", :"l"(cell),"r"(b))
GLOBAL32(red_max_s32, "red.global.max.s32 [%0],%1;", :"l"(cell),"r"(b))
GLOBAL32(red_add_f32, "red.global.add.f32 [%0],%1;", :"l"(cell),"r"(b))
extern "C" __global__ void atom_generic_add_u32(const uint32_t *in, uint64_t *out) {
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r;
 __shared__ uint32_t cells[1024]; uint32_t *cell=c&1?&cells[threadIdx.x]:(uint32_t *)(out+i)+1; *(volatile uint32_t *)cell=a;
 asm volatile("atom.add.u32 %0,[%1],%2;":"=r"(r):"l"(cell),"r"(b):"memory");
 out[i]=(uint64_t)*(volatile uint32_t *)cell<<32|r;
}
SHARED32(shared_add_u32, "atom.shared.add.u32 %0,[%1],%2;", "=r"(r):"r"(s),"r"(b))
SHARED32(shared_min_s32, "atom.shared.min.s32 %0,[%1],%2;", "=r"(r):"r"(s),"r"(b))
SHARED32(shared_inc_u32, "atom.shared.inc.u32 %0,[%1],%2;", "=r"(r):"r"(s),"r"(b))
SHARED32(shared_exch_b32, "atom.shared.exch.b32 %0,[%1],%2;", "=r"(r):"r"(s),"r"(b))
SHARED32(shared_cas_b32, "atom.shared.cas.b32 %0,[%1],%2,%3;", "=r"(r):"r"(s),"r"(b),"r"(c))
GLOBAL64(add_u64, "atom.global.add.u64 %0,[%1],%2;", "=l"(r):"l"(cell),"l"(b))
GLOBAL64(min_s64, "atom.global.min.s64 %0,[%1],%2;", "=l"(r):"l"(cell),"l"(b))
GLOBAL64(max_u64, "atom.global.max.u64 %0,[%1],%2;", "=l"(r):"l"(cell),"l"(b))
GLOBAL64(and_b64, "atom.global.and.b64 %0,[%1],%2;", "=l"(r):"l"(cell),"l"(b))
GLOBAL64(exch_b64, "atom.global.exch.b64 %0,[%1],%2;", "=l"(r):"l"(cell),"l"(b))
GLOBAL64(cas_b64, "atom.global.cas.b64 %0,[%1],%2,%3;", "=l"(r):"l"(cell),"l"(b),"l"(c))
extern "C" __global__ void atom_red_add_f64(const uint64_t *in, uint64_t *out) {
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint64_t a=in[3*i],b=in[3*i+1],r;
 uint64_t *cell=out+i; *(volatile uint64_t *)cell=a;
 asm volatile("atom.global.add.f64 %0,[%1],%2;":"=l"(r):"l"(cell),"l"(b):"memory"); (void)r;
}
