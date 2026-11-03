#include <stdint.h>

__device__ unsigned thread_rank() {
 return threadIdx.x + blockDim.x * (threadIdx.y + blockDim.y * threadIdx.z);
}
__device__ unsigned block_rank() {
 return blockIdx.x + gridDim.x * (blockIdx.y + gridDim.y * blockIdx.z);
}
__device__ unsigned block_size() { return blockDim.x * blockDim.y * blockDim.z; }
__device__ unsigned index() { return thread_rank() + block_size() * block_rank(); }

#define COORD(name, value) extern "C" __global__ void name(const uint32_t *in, uint32_t *out) { out[index()] = value; }
COORD(tid_x, threadIdx.x)
COORD(tid_y, threadIdx.y)
COORD(tid_z, threadIdx.z)
COORD(ctaid_x, blockIdx.x)
COORD(ctaid_y, blockIdx.y)
COORD(ctaid_z, blockIdx.z)
COORD(block_x, blockDim.x)
COORD(block_y, blockDim.y)
COORD(block_z, blockDim.z)
COORD(grid_x, gridDim.x)
COORD(grid_y, gridDim.y)
COORD(grid_z, gridDim.z)
extern "C" __global__ void laneid(const uint32_t *in, uint32_t *out) {
 unsigned lane;
 asm volatile("mov.u32 %0, %%laneid;" : "=r"(lane));
 out[index()] = lane;
}

extern "C" __global__ void partial(const uint32_t *in, uint64_t *out) {
 __shared__ uint32_t s[64];
 unsigned t = thread_rank(), i = index(), a = in[3*i], mask = __activemask();
 s[t] = a; __syncthreads();
 uint32_t last = s[block_size()-1]; __syncthreads();
 s[t] = last ^ a;
 __syncwarp(mask);
 uint32_t first = s[t & ~31u]; __syncthreads();
 unsigned ballot = __ballot_sync(mask, a & 1);
 unsigned shuffled = __shfl_sync(mask, a, 0);
 out[i] = (uint64_t)(uint32_t)(s[0] + first + shuffled) << 32 | ballot;
}
