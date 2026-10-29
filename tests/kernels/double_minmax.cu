#include <math.h>
#include <stdint.h>

// Runtime inputs prevent folding. Compile with -O3 without --use_fast_math.
extern "C" __global__ void double_fmin(const uint64_t *input, uint64_t *output) {
    unsigned i = blockIdx.x * blockDim.x + threadIdx.x;
    double a = __longlong_as_double(input[3 * i]);
    double b = __longlong_as_double(input[3 * i + 1]);
    output[i] = __double_as_longlong(fmin(a, b));
}

extern "C" __global__ void double_fmax(const uint64_t *input, uint64_t *output) {
    unsigned i = blockIdx.x * blockDim.x + threadIdx.x;
    double a = __longlong_as_double(input[3 * i]);
    double b = __longlong_as_double(input[3 * i + 1]);
    output[i] = __double_as_longlong(fmax(a, b));
}
