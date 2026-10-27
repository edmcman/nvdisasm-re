// Every backend template is instantiated, including all nine conversion pairs.
#include "sass/fp.hpp"
#include "sass/bits.hpp"

template <int W> __device__ uint64_t arithmetic(const uint64_t *r) {
  const auto mode = sass::fp::Rnd(r[3]);
  const uint64_t a = r[5], b = r[6], c = r[7];
  switch (r[0]) {
    case 0: return sass::fp::add<W>(a, b, mode);
    case 1: return sass::fp::mul<W>(a, b, mode, int(r[4]));
    case 2: return sass::fp::fma<W>(a, b, c, mode);
    case 3:
      switch (r[2]) {
        case 16: return sass::fp::convert<W, 16>(a, mode);
        case 32: return sass::fp::convert<W, 32>(a, mode);
        default: return sass::fp::convert<W, 64>(a, mode);
      }
    case 4: return sass::fp::lt<W>(a, b);
    case 5: return sass::fp::le<W>(a, b);
    case 6: return sass::fp::eq<W>(a, b);
    default: return sass::fp::isnan<W>(a);
  }
}

extern "C" __global__ void fp_backends(const uint64_t *requests, uint64_t *out) {
  const int i = blockIdx.x * blockDim.x + threadIdx.x;
  const uint64_t *r = requests + 8 * i;
  // CUDA takes precedence even when SASS_FP_SOFT is defined by the smoke build.
  static_assert(sass::fp::RN == 0 && sass::fp::RZ == 1 && sass::fp::RP == 2 && sass::fp::RM == 3);
  uint64_t result = r[1] == 16 ? arithmetic<16>(r) : r[1] == 32 ? arithmetic<32>(r) : arithmetic<64>(r);
  // Instantiate device bit helpers, including the 128-bit storage path.
  const auto wide = sass::sext<64, 128>(result);
  result = sass::wrap<64>(sass::shr<128>(sass::shl<128>(wide, 1), 1));
  out[i] = result;
}
