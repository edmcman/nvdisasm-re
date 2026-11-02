#include <cuda_fp16.h>
#include <stdint.h>

extern "C" __global__ void half_widen(const uint32_t *input, uint32_t *output) {
    unsigned i = blockIdx.x * blockDim.x + threadIdx.x;
    output[i] = __float_as_uint(__half2float(__ushort_as_half(input[3 * i])));
}
