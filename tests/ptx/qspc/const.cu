#include <cuda_runtime.h>
#include <cstdint>

__global__ void qspc_const_probe(const uint32_t *candidate,
                                uint32_t *result) {
    uint32_t is_constant;
    asm volatile(
        "{ .reg .pred p; isspacep.const p, %1; selp.u32 %0, 1, 0, p; }"
        : "=r"(is_constant) : "l"(candidate));
    if (threadIdx.x == 0) result[blockIdx.x] = is_constant;
}
