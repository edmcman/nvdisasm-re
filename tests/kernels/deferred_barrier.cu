#include <stdint.h>
extern "C" __global__ void sem_deferred_order(const uint32_t *in, uint64_t *out) {
  unsigned t = threadIdx.x;
  if (t >= 32) {
    uint64_t start = clock64();
    while (clock64() - start < 200000) {}
  }
  uint64_t before, after;
  asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(before));
  asm volatile("bar.sync 0;" ::: "memory");
  asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(after));
  out[t] = t < 32 ? after : before;
}

extern "C" __global__ void sem_deferred_load(const uint32_t *in, uint64_t *out) {
  __shared__ uint32_t shared[64];
  unsigned t = threadIdx.x;
  shared[t] = 0;
  if (t >= 32) {
    uint64_t start = clock64();
    while (clock64() - start < 200000) {}
  }
  uint64_t before, after;
  uint32_t value;
  asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(before));
  asm volatile("bar.sync 0;" ::: "memory");
  asm volatile("ld.shared.u32 %0, [%1];" : "=r"(value) : "r"((uint32_t)__cvta_generic_to_shared(&shared[0])));
  asm volatile("mov.u64 %0, %%globaltimer;" : "=l"(after));
  out[t] = (t < 32 ? after : before) + value;
}
