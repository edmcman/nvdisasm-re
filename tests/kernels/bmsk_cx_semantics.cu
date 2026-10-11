#include <stdint.h>

// Compiler-generated wrapper for a manually patched SASS experiment. The
// oracle validates the wrapper before replacing four BARs with uniform header
// construction, the BMSK with its CX variant, and the XOR with NOP.
extern "C" __global__ void sem_cx(const uint32_t *in, uint32_t *out) {
    unsigned i = threadIdx.x + blockIdx.x * blockDim.x;
    uint32_t a = in[3*i], r;
    uint64_t header = ((uint64_t)in >> 4) | ((uint64_t)128 << 45);
    uint32_t lo = header, hi = header >> 32;
    asm volatile("bmsk.clamp.b32 %0,%1,%2; xor.b32 %0,%0,%3; "
                 "bar.sync 0; bar.sync 0; bar.sync 0; bar.sync 0;"
                 : "=r"(r) : "r"(a), "r"(lo), "r"(hi) : "memory");
    out[i] = r;
}
