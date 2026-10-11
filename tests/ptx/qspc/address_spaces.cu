#include <cuda_runtime.h>
#include <cstdint>
#include <cstdio>

__global__ void qspc_probe(const uint64_t *candidates, uint32_t *results) {
    __shared__ uint32_t shared_value;
    uint32_t local_value = 0;
    uint64_t candidate = candidates[threadIdx.x];

    asm volatile(
        "{ .reg .pred p; isspacep.global p, %1; selp.u32 %0, 1, 0, p; }"
        : "=r"(results[threadIdx.x]) : "l"(candidate));
    if (threadIdx.x == 0) {
        uint32_t local_query, local_as_global, shared_query, shared_as_global;
        asm volatile(
            "{ .reg .b64 s, g; .reg .pred p; cvta.to.local.u64 s, %2; "
            "cvta.local.u64 g, s; isspacep.local p, g; "
            "selp.u32 %0, 1, 0, p; isspacep.global p, g; "
            "selp.u32 %1, 1, 0, p; }"
            : "=r"(local_query), "=r"(local_as_global)
            : "l"(&local_value));
        asm volatile(
            "{ .reg .b64 s, g; .reg .pred p; cvta.to.shared.u64 s, %2; "
            "cvta.shared.u64 g, s; isspacep.shared p, g; "
            "selp.u32 %0, 1, 0, p; isspacep.global p, g; "
            "selp.u32 %1, 1, 0, p; }"
            : "=r"(shared_query), "=r"(shared_as_global)
            : "l"(&shared_value));
        results[2] = local_query;
        results[3] = shared_query;
        results[4] = local_as_global;
        results[5] = shared_as_global;
    }
}

int main() {
    uint64_t *candidates = nullptr;
    uint32_t *results = nullptr;
    if (cudaMalloc(&candidates, 2 * sizeof(uint64_t)) != cudaSuccess ||
        cudaMalloc(&results, 6 * sizeof(uint32_t)) != cudaSuccess) return 2;
    uint64_t host_candidates[2] = {
        reinterpret_cast<uint64_t>(candidates), 1
    };
    if (cudaMemcpy(candidates, host_candidates, sizeof(host_candidates),
                   cudaMemcpyHostToDevice) != cudaSuccess) return 3;
    qspc_probe<<<1, 2>>>(candidates, results);
    uint32_t host_results[6] = {};
    if (cudaGetLastError() != cudaSuccess ||
        cudaDeviceSynchronize() != cudaSuccess ||
        cudaMemcpy(host_results, results, sizeof(host_results),
                   cudaMemcpyDeviceToHost) != cudaSuccess) return 4;
    cudaFree(results);
    cudaFree(candidates);
    std::printf("global-valid=%u global-low=%u local=%u shared=%u local-as-global=%u shared-as-global=%u\n",
                host_results[0], host_results[1], host_results[2],
                host_results[3], host_results[4], host_results[5]);
    return host_results[0] == 1 && host_results[1] == 1 &&
           host_results[2] == 1 && host_results[3] == 1 &&
           host_results[4] == 0 && host_results[5] == 0 ? 0 : 1;
}
