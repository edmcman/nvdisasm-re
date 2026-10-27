// Exact FP16 conversions using at most 64-bit integer arithmetic, shared with CUDA.
#pragma once
#include "sass/fp_common.hpp"

namespace sass::fp_convert {
using namespace fp_common;

SASS_HD inline int leading(u64 bits) {
#ifdef __CUDA_ARCH__
  return 63 - __clzll(bits);
#else
  return 63 - std::countl_zero(bits);
#endif
}

template <int W> SASS_HD inline u64 identity(u64 bits) {
  static_assert(W == 16 || W == 32 || W == 64);
  constexpr int M = W == 16 ? 10 : W == 32 ? 23 : 52;
  constexpr u64 sign = u64(1) << (W - 1);
  constexpr u64 infinity = (sign - 1) & ~((u64(1) << M) - 1);
  return (bits & (sign - 1)) > infinity
           ? (bits & sign) | infinity | (u64(1) << (M - 1))
           : bits & (sign | (sign - 1));
}

// Every half value is exact in float/double; only half subnormals need normalization.
template <int D> SASS_HD inline u64 half_to(u64 bits) {
  static_assert(D == 32 || D == 64);
  constexpr int M = D == 32 ? 23 : 52, B = D == 32 ? 127 : 1023;
  const u64 sign = (bits & 0x8000) << (D - 16), fraction = bits & 0x3ff;
  const unsigned exponent = (bits >> 10) & 0x1f;
  if (exponent == 31)
    return sign | u64(2 * B + 1) << M | (fraction ? u64(1) << (M - 1) : 0);
  if (exponent) return sign | u64(exponent + B - 15) << M | fraction << (M - 10);
  if (!fraction) return sign;
  const int top = leading(fraction);
  return sign | u64(top - 24 + B) << M | (fraction - (u64(1) << top)) << (M - top);
}

SASS_HD inline u64 half_overflow(u64 sign, Rnd mode) {
  const bool infinite = mode == RN || (mode == RP && !sign) || (mode == RM && sign);
  return sign | (infinite ? 0x7c00 : 0x7bff);
}

// Round once to half, optionally scaling the exact source value by 2^scale.
// Early exponent bounds keep every remaining rounding shift within 1..53.
template <int S> SASS_HD inline u64 to_half(u64 bits, Rnd mode, int scale = 0) {
  static_assert(S == 32 || S == 64);
  constexpr int M = S == 32 ? 23 : 52, B = S == 32 ? 127 : 1023;
  constexpr u64 fraction_mask = (u64(1) << M) - 1;
  const u64 sign = (bits >> (S - 16)) & 0x8000, fraction = bits & fraction_mask;
  const unsigned exponent = (bits >> M) & (2 * B + 1);
  if (exponent == 2 * B + 1) return sign | (fraction ? 0x7e00 : 0x7c00);
  if (!exponent && !fraction) return sign;
  const u64 sig = fraction | (exponent ? u64(1) << M : 0);
  const int64_t power = int64_t(exponent ? int(exponent) - B : 1 - B) - M + scale;
  int64_t e = (exponent ? M : leading(sig)) + power;
  if (e > 15) return half_overflow(sign, mode);
  if (e < -25) return sign | ((mode == RP && !sign) || (mode == RM && sign));
  const int shift = int((e > -14 ? e : -14) - 10 - power);
  u64 q;
  if (shift <= 0) q = sig << -shift;
  else {
    q = sig >> shift;
    const u64 rest = sig & ((u64(1) << shift) - 1), half = u64(1) << (shift - 1);
    const bool up = mode == RN ? rest > half || (rest == half && (q & 1))
                    : mode == RP ? rest && !sign : mode == RM ? rest && sign : false;
    q += up;
  }
  if (q >= 2048) { q >>= 1; ++e; }
  if (e > 15) return half_overflow(sign, mode);
  if (q < 1024) return sign | q;
  return sign | u64((e > -14 ? e : -14) + 15) << 10 | (q - 1024);
}

// Exact rescaling of a normal operand before a single native multiply.
// Returning false leaves extreme exponents and subnormals to the wider fallback.
template <int W> SASS_HD inline bool scale_normal(u64 bits, int scale, u64 &result) {
  static_assert(W == 32 || W == 64);
  constexpr int M = W == 32 ? 23 : 52, E = W == 32 ? 255 : 2047;
  const int exponent = (bits >> M) & E;
  const int64_t scaled = int64_t(exponent) + scale;
  if (!exponent || exponent == E || scaled <= 0 || scaled >= E) return false;
  result = (bits & ((u64(1) << (W - 1)) | ((u64(1) << M) - 1))) | u64(scaled) << M;
  return true;
}
}  // namespace sass::fp_convert
