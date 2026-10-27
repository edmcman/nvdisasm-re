// Strict native arithmetic. Compile with dynamic rounding and contraction disabled.
#pragma once
#include "sass/fp_convert.hpp"
#include "sass/fp_multiply.hpp"
#ifndef __CUDA_ARCH__
#include <cfenv>
#include <cmath>
#include <stdexcept>
#ifndef __SSE2__
#error "sass::fp_host requires x86 SSE2"
#endif
#pragma STDC FENV_ACCESS ON
#pragma STDC FP_CONTRACT OFF

namespace sass::fp_host {
using namespace fp_common;

class Rounding {
  int saved;
  bool changed;
public:
  explicit SASS_FORCEINLINE Rounding(Rnd mode) : saved(std::fegetround()), changed(false) {
    const int modes[] = {FE_TONEAREST, FE_TOWARDZERO, FE_UPWARD, FE_DOWNWARD};
    changed = saved != modes[mode];
    if (saved == -1 || (changed && std::fesetround(modes[mode])))
      throw std::runtime_error("sass::fp_host cannot establish IEEE rounding");
  }
  ~Rounding() { if (changed) std::fesetround(saved); }
  Rounding(const Rounding &) = delete;
  Rounding &operator=(const Rounding &) = delete;
};

template <int W> SASS_HD inline u64 canonical(u64 bits) {
  return is_nan<W>(bits) ? nan_bits<W>() : bits;
}

SASS_HD inline double half(u64 bits) {
  return value<64>(fp_convert::half_to<64>(bits));
}

// Both inputs are prepared normal operands with combined exponent below minimum.
// Round their exact product on the subnormal grid, avoiding SSE's tiny-result penalty.
template <int W> SASS_HD SASS_FORCEINLINE u64 tiny_product(u64 a, u64 b, Rnd mode) {
  constexpr int M = fraction_bits<W>(), B = W == 32 ? 127 : 1023;
  constexpr u64 fraction = (u64(1) << M) - 1;
  using Wide = std::conditional_t<W == 32, u64, unsigned __int128>;
  const int e = int((a >> M) & (2 * B + 1)) + int((b >> M) & (2 * B + 1)) - 2 * B;
  const int shift = M + (1 - B) - e;  // bounded by M+1 .. 2M+2
  const Wide product = Wide((a & fraction) | (u64(1) << M)) * ((b & fraction) | (u64(1) << M));
  const u64 q = product >> shift, sign = (a ^ b) & (u64(1) << (W - 1));
  const bool guard = (product >> (shift - 1)) & 1;
  const bool sticky = (product & ((Wide(1) << (shift - 1)) - 1)) != 0;
  const bool up = mode == RN ? guard && (sticky || (q & 1))
                  : mode == RP ? (guard || sticky) && !sign
                  : mode == RM ? (guard || sticky) && sign : false;
  // q may carry into the first normal binade; its encoding still equals q.
  return sign | (q + up);
}

// TwoSum gives s + residual exactly. RN and no contraction are required by its proof.
// Jamming the toward-zero intermediate to odd avoids double rounding to FP16.
struct OddSum { double sum, residual, odd; };
SASS_HD inline OddSum round_odd_sum(double product, double addend) {
  volatile double p = product, c = addend;
  volatile double s = p + c;
  volatile double v = s - p;
  volatile double sp = s - v;
  volatile double ep = p - sp;
  volatile double ec = c - v;
  volatile double residual = ep + ec;
  double odd = s;
  if (residual != 0) {
    if ((residual < 0) != (s < 0)) odd = std::nextafter(odd, 0.0);
    odd = value<64>(encoding<64>(odd) | 1);
  }
  return {s, residual, odd};
}

template <int W> SASS_HD inline u64 add(u64 a, u64 b, Rnd mode) {
  if constexpr (W == 16) {
    // All half sums fit exactly in double; zero signs are handled explicitly below.
    volatile double x = half(a), y = half(b), result = x + y;
    u64 bits = canonical<64>(encoding<64>(result));
    if ((bits & 0x7fffffffffffffffULL) == 0) {
      const int sign = (a & 0x7fff) == 0 && (b & 0x7fff) == 0 && (a >> 15) == (b >> 15)
                       ? a >> 15 : mode == RM;
      return u64(sign) << 15;
    }
    return fp_convert::to_half<64>(bits, mode);
  } else {
    Rounding rounding(mode);
    volatile Native<W> x = value<W>(a), y = value<W>(b), result = x + y;
    return canonical<W>(encoding<W>(result));
  }
}

template <int W> SASS_HD SASS_FORCEINLINE u64 mul(u64 a, u64 b, Rnd mode, int scale) {
  if constexpr (W == 16) {
    // The 22-bit product is exact in double in every caller rounding mode.
    volatile double x = half(a), y = half(b), product = x * y;
    return fp_convert::to_half<64>(canonical<64>(encoding<64>(product)), mode, scale);
  } else {
    u64 scaled_a = a, scaled_b = b;
    if (scale && !fp_convert::scale_normal<W>(a, scale, scaled_a)
              && !fp_convert::scale_normal<W>(b, scale, scaled_b)) {
      const auto operands = fp_multiply::prepare<W>(a, b, mode, scale);
      if (!operands.multiply) return operands.result;
      scaled_a = operands.a;
      scaled_b = operands.b;
      constexpr int M = fraction_bits<W>(), B = W == 32 ? 127 : 1023;
      if (int((scaled_a >> M) & (2 * B + 1)) + int((scaled_b >> M) & (2 * B + 1)) < B + 1)
        return tiny_product<W>(scaled_a, scaled_b, mode);
    }
    Rounding rounding(mode);
    volatile Native<W> x = value<W>(scaled_a), y = value<W>(scaled_b), result = x * y;
    return canonical<W>(encoding<W>(result));
  }
}

template <int W> SASS_HD inline u64 fma(u64 a, u64 b, u64 c, Rnd mode) {
  if constexpr (W == 16) {
    const u64 x = a & 0x7fff, y = b & 0x7fff, z = c & 0x7fff, sign = (a ^ b) & 0x8000;
    if (x > 0x7c00 || y > 0x7c00 || z > 0x7c00) return 0x7e00;
    if ((x == 0x7c00 && !y) || (y == 0x7c00 && !x)) return 0x7e00;
    if (x == 0x7c00 || y == 0x7c00)
      return z == 0x7c00 && ((c ^ sign) & 0x8000) ? 0x7e00 : sign | 0x7c00;
    if (z == 0x7c00) return c;
    Rounding rounding(RN);
    volatile double av = half(a), bv = half(b), product = av * bv;
    const auto sum = round_odd_sum(product, half(c));
    if (sum.sum == 0 && sum.residual == 0) {
      return ((!x || !y) && !z && !((c ^ sign) & 0x8000)) ? sign : mode == RM ? 0x8000 : 0;
    }
    return fp_convert::to_half<64>(encoding<64>(sum.odd), mode);
  } else {
    Rounding rounding(mode);
    volatile Native<W> x = value<W>(a), y = value<W>(b), z = value<W>(c);
    volatile Native<W> result = std::fma(x, y, z);
    return canonical<W>(encoding<W>(result));
  }
}

template <int S, int D> SASS_HD inline u64 convert(u64 bits, Rnd mode) {
  if constexpr (S == D) return fp_convert::identity<S>(bits);
  else if constexpr (S == 16) return fp_convert::half_to<D>(bits);
  else if constexpr (D == 16) return fp_convert::to_half<S>(bits, mode);
  else {
    if (is_nan<S>(bits)) return (bits >> (S - 1)) << (D - 1) | nan_bits<D>();
    Rounding rounding(mode);
    volatile Native<S> source = value<S>(bits);
    volatile Native<D> result = Native<D>(source);
    return encoding<D>(result);
  }
}

template <int W> SASS_HD inline bool isnan(u64 bits) { return is_nan<W>(bits); }
template <int W> SASS_HD inline auto comparison_value(u64 bits) {
  if constexpr (W == 16) return half(bits);
  else return value<W>(bits);
}
template <int W> SASS_HD inline bool lt(u64 a, u64 b) {
  return !isnan<W>(a) && !isnan<W>(b) && comparison_value<W>(a) < comparison_value<W>(b);
}
template <int W> SASS_HD inline bool le(u64 a, u64 b) {
  return !isnan<W>(a) && !isnan<W>(b) && comparison_value<W>(a) <= comparison_value<W>(b);
}
template <int W> SASS_HD inline bool eq(u64 a, u64 b) {
  return !isnan<W>(a) && !isnan<W>(b) && comparison_value<W>(a) == comparison_value<W>(b);
}
}  // namespace sass::fp_host
#endif
