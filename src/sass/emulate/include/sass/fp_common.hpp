// Shared ABI and annotations for bit-encoded floating-point backends.
#pragma once
#include <bit>
#include <cstdint>
#include <type_traits>

#ifdef __CUDACC__
#define SASS_HD __host__ __device__
#else
#define SASS_HD
#endif

namespace sass::fp_common {
enum Rnd { RN = 0, RZ = 1, RP = 2, RM = 3 };
using u64 = uint64_t;
template <int W> using Native = std::conditional_t<W == 32, float, double>;

template <int W> SASS_HD Native<W> value(u64 bits) {
  static_assert(W == 32 || W == 64);
#ifdef __CUDA_ARCH__
  if constexpr (W == 32) return __uint_as_float(uint32_t(bits));
  else return __longlong_as_double(bits);
#else
  if constexpr (W == 32) return std::bit_cast<float>(uint32_t(bits));
  else return std::bit_cast<double>(bits);
#endif
}

template <int W> SASS_HD u64 encoding(Native<W> value) {
  static_assert(W == 32 || W == 64);
#ifdef __CUDA_ARCH__
  if constexpr (W == 32) return __float_as_uint(value);
  else return __double_as_longlong(value);
#else
  if constexpr (W == 32) return std::bit_cast<uint32_t>(value);
  else return std::bit_cast<u64>(value);
#endif
}
}  // namespace sass::fp_common
