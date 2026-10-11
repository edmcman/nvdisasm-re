#include <cuda_runtime.h>
#include <cstdint>

// Compile with -arch=sm_90. The SM89 test device cannot execute this probe.
__global__ void qspc_cluster_probe(uint32_t *result) {
    __shared__ uint32_t shared_value;
    uint32_t is_cluster_shared;
    asm volatile(
        "{ .reg .b64 s, g; .reg .pred p; cvta.to.shared.u64 s, %1; "
        "cvta.shared.u64 g, s; isspacep.shared::cluster p, g; "
        "selp.u32 %0, 1, 0, p; }"
        : "=r"(is_cluster_shared) : "l"(&shared_value));
    if (threadIdx.x == 0) result[blockIdx.x] = is_cluster_shared;
}
