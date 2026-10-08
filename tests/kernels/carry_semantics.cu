#include <stdint.h>

// Reserve eight BAR words around a placeholder arithmetic operation. The
// hardware oracle validates the compiler's register and slot assignments.
extern "C" __global__ void sem_probe(const uint32_t *in, uint32_t *out) {
    unsigned i = threadIdx.x + blockIdx.x * blockDim.x;
    uint32_t a = in[3*i], b = in[3*i+1], c = in[3*i+2], r;
    asm volatile(
        "mad.lo.u32 %0,%1,%2,%3; "
        "bar.sync 0; bar.sync 0; bar.sync 0; bar.sync 0; "
        "bar.sync 0; bar.sync 0; bar.sync 0; bar.sync 0;"
        : "=r"(r) : "r"(a), "r"(b), "r"(c) : "memory");
    out[i] = r;
}
