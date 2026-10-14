#include <stdint.h>
// Whole-warp collectives (all 32 lanes active); out[i] = result | predicate(s) << 32.
#define SHFL(mode) extern "C" __global__ void sem_shfl_##mode(const uint32_t *in, uint64_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r,p; \
 asm volatile("{ .reg .pred q; shfl.sync." #mode ".b32 %0|q,%2,%3,%4,0xffffffff; selp.u32 %1,1,0,q; }" \
              :"=r"(r),"=r"(p):"r"(a),"r"(b),"r"(c)); \
 out[i]=(uint64_t)p<<32|r; }
SHFL(idx)
SHFL(up)
SHFL(down)
SHFL(bfly)
extern "C" __global__ void sem_vote(const uint32_t *in, uint64_t *out) {
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],ballot,any,all,uni;
 asm volatile("{ .reg .pred q,x,y,z; setp.ne.u32 q,%4,0; vote.sync.ballot.b32 %0,q,0xffffffff;"
              " vote.sync.any.pred x,q,0xffffffff; vote.sync.all.pred y,q,0xffffffff; vote.sync.uni.pred z,q,0xffffffff;"
              " selp.u32 %1,1,0,x; selp.u32 %2,1,0,y; selp.u32 %3,1,0,z; }"
              :"=r"(ballot),"=r"(any),"=r"(all),"=r"(uni):"r"(a&1));
 out[i]=(uint64_t)(any|all<<1|uni<<2)<<32|ballot;
}
#define WARP(name, body) extern "C" __global__ void sem_##name(const uint32_t *in, uint64_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],b=in[3*i+1],c=in[3*i+2],r=0,p=0; body; out[i]=(uint64_t)p<<32|r; }
WARP(match_any, asm volatile("match.any.sync.b32 %0,%1,0xffffffff;":"=r"(r):"r"(a)))
WARP(match_any64, asm volatile("match.any.sync.b64 %0,%1,0xffffffff;":"=r"(r):"l"((uint64_t)b<<32|a)))
WARP(match_all, asm volatile("{ .reg .pred q; match.all.sync.b32 %0|q,%2,0xffffffff; selp.u32 %1,1,0,q; }":"=r"(r),"=r"(p):"r"(a)))
#define REDUX(name, op) WARP(redux_##name, asm volatile("redux.sync." op " %0,%1,0xffffffff;":"=r"(r):"r"(a)))
REDUX(add, "add.u32") REDUX(min_u32, "min.u32") REDUX(min_s32, "min.s32") REDUX(max_u32, "max.u32") REDUX(max_s32, "max.s32")
REDUX(and, "and.b32") REDUX(or, "or.b32") REDUX(xor, "xor.b32")
// VOTEU.ANY UR, UPT, PT (activemask); tests.cases.threads hoists the predicate above it and patches Pp.
#define VOTEU(name) WARP(name, asm volatile("{ .reg .pred q,x; setp.ne.u32 q,%2,0; activemask.b32 %0; vote.sync.all.pred x,q,0xffffffff; selp.u32 %1,1,0,x; }" \
 :"=r"(r),"=r"(p):"r"(c & 1)))
VOTEU(voteu) VOTEU(voteu_not)
// Four warps exchange values through shared memory across barriers.
extern "C" __global__ void sem_shared_exchange(const uint32_t *in, uint32_t *out) {
 __shared__ uint32_t s[128];
 unsigned t=threadIdx.x, i=t+blockIdx.x*blockDim.x; uint32_t a=in[3*i], b=in[3*i+1];
 s[t]=a; __syncthreads();
 uint32_t r=s[(t+b)&127]; __syncthreads();
 s[t]=r*3+1; __syncthreads();
 out[i]=r^s[(t*5+7)&127];
}
