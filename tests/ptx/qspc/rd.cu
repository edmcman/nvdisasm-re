#include <cuda_runtime.h>
#include <cstdint>

__global__ void qspc_rd_probe(const uint64_t *candidate, uint32_t seed,
                              uint32_t *result) {
    uint32_t query;
    asm volatile(
        "{ .reg .pred p; isspacep.global p, %1; selp.u32 %0, 1, 0, p; }"
        : "=r"(query) : "l"(candidate[0]));
    result[0] = query;
    result[1] = seed;
}

__global__ void qspc_rd_local(uint32_t seed, uint32_t *result) {
    uint32_t local_value = seed;
    uint64_t generic_address;
    uint32_t query;
    asm volatile("{ .reg .b64 s; cvta.to.local.u64 s, %1; "
                 "cvta.local.u64 %0, s; }"
                 : "=l"(generic_address) : "l"(&local_value));
    asm volatile(
        "{ .reg .pred p; isspacep.local p, %1; selp.u32 %0, 1, 0, p; }"
        : "=r"(query) : "l"(generic_address));
    result[0] = query;
    result[1] = seed;
}

__global__ void qspc_rd_shared(uint32_t seed, uint32_t *result) {
    __shared__ uint32_t shared_value;
    uint64_t generic_address;
    uint32_t query;
    shared_value = seed;
    asm volatile("{ .reg .b64 s; cvta.to.shared.u64 s, %1; "
                 "cvta.shared.u64 %0, s; }"
                 : "=l"(generic_address) : "l"(&shared_value));
    asm volatile(
        "{ .reg .pred p; isspacep.shared p, %1; selp.u32 %0, 1, 0, p; }"
        : "=r"(query) : "l"(generic_address));
    result[0] = query;
    result[1] = seed;
}
