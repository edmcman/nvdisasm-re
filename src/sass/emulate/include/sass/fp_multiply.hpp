// Exact operand preparation for a single native scaled multiplication.
#pragma once
#include "sass/fp_convert.hpp"

namespace sass::fp_multiply {
using namespace fp_common;

template <int W> SASS_HD inline u64 overflow(u64 sign, Rnd mode) {
  const bool infinite = mode == RN || (mode == RP && !sign) || (mode == RM && sign);
  return sign | (infinite ? infinity_bits<W>() : infinity_bits<W>() - 1);
}

struct Operands { u64 a, b, result; bool multiply; };

// Write a = sa * 2^ea, b = sb * 2^eb with 1 <= |sa|, |sb| < 2.
// Redistribute ea + eb + scale between two normal operands. Their exact product
// equals the requested scaled product, so native multiplication rounds only once,
// even when the result is subnormal. Classification handles exponents too extreme
// to affect a rounding boundary. int64_t permits every signed int scale.
template <int W> SASS_HD SASS_FORCEINLINE Operands prepare(u64 a, u64 b, Rnd mode, int scale) {
  static_assert(W == 32 || W == 64);
  constexpr int M = fraction_bits<W>(), B = W == 32 ? 127 : 1023, minimum = 1 - B;
  constexpr u64 sign_mask = u64(1) << (W - 1), fraction_mask = (u64(1) << M) - 1;
  const u64 sign = (a ^ b) & sign_mask, x = a & (sign_mask - 1), y = b & (sign_mask - 1);
  if (x > infinity_bits<W>() || y > infinity_bits<W>()) return {0, 0, nan_bits<W>(), false};
  if (x == infinity_bits<W>() || y == infinity_bits<W>())
    return {0, 0, !x || !y ? nan_bits<W>() : sign | infinity_bits<W>(), false};
  if (!x || !y) return {0, 0, sign, false};
  const int ax = x >> M, bx = y >> M;
  u64 av = (x & fraction_mask) | (ax ? u64(1) << M : 0);
  u64 bv = (y & fraction_mask) | (bx ? u64(1) << M : 0);
  const int ashift = ax ? 0 : M - fp_convert::leading(av);
  const int bshift = bx ? 0 : M - fp_convert::leading(bv);
  av <<= ashift;
  bv <<= bshift;
  const int64_t e = int64_t(ax ? ax - B : minimum - ashift)
                  + (bx ? bx - B : minimum - bshift) + scale;
  if (e > B) return {0, 0, overflow<W>(sign, mode), false};
  // Since |sa * sb| < 4, this bound is strictly below half a minimum subnormal.
  if (e < minimum - M - 2)
    return {0, 0, sign | ((mode == RP && !sign) || (mode == RM && sign)), false};
  // e is now in [minimum-M-2, B]. Both distributed exponents are normal:
  // ae in [minimum, B], be in [-M-2, 0].
  const int ae = int(e < minimum ? minimum : e), be = int(e - ae);
  return {(a & sign_mask) | u64(ae + B) << M | (av & fraction_mask),
          (b & sign_mask) | u64(be + B) << M | (bv & fraction_mask), 0, true};
}
}  // namespace sass::fp_multiply
