#include <stdint.h>
#define KERNEL(name, mode) extern "C" __global__ void sem_bmsk_##name(const uint32_t *in, uint32_t *out) { \
 unsigned i=threadIdx.x+blockIdx.x*blockDim.x; uint32_t a=in[3*i],b=in[3*i+1],r; \
 asm("bmsk." mode ".b32 %0,%1,%2;":"=r"(r):"r"(a),"r"(b)); out[i]=r; }
KERNEL(c, "clamp")
KERNEL(w, "wrap")
