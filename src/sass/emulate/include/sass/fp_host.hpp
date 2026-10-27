// Strict native arithmetic. Compile with dynamic rounding and contraction disabled.
#pragma once
#include "sass/softfloat.hpp"
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
public:
  explicit Rounding(Rnd mode) : saved(std::fegetround()) {
    const int modes[] = {FE_TONEAREST, FE_TOWARDZERO, FE_UPWARD, FE_DOWNWARD};
    if (saved == -1 || std::fesetround(modes[mode]))
      throw std::runtime_error("sass::fp_host cannot establish IEEE rounding");
  }
  ~Rounding() { std::fesetround(saved); }
  Rounding(const Rounding &) = delete;
  Rounding &operator=(const Rounding &) = delete;
};

template <int W> SASS_HD inline u64 canonical(u64 bits) {
  return softfloat::isnan<W>(bits) ? softfloat::nan<W>() : bits;
}

SASS_HD inline double half(u64 bits) {
  return value<64>(softfloat::convert<16, 64>(bits, RN));
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
    Rounding rounding(RN);
    volatile double x = half(a), y = half(b), result = x + y;
    u64 bits = canonical<64>(encoding<64>(result));
    if ((bits & 0x7fffffffffffffffULL) == 0) {
      const int sign = (a & 0x7fff) == 0 && (b & 0x7fff) == 0 && (a >> 15) == (b >> 15)
                       ? a >> 15 : mode == RM;
      return u64(sign) << 15;
    }
    return softfloat::convert<64, 16>(bits, mode);
  } else {
    Rounding rounding(mode);
    volatile Native<W> x = value<W>(a), y = value<W>(b), result = x + y;
    return canonical<W>(encoding<W>(result));
  }
}

template <int W> SASS_HD inline u64 mul(u64 a, u64 b, Rnd mode, int scale) {
  if constexpr (W == 64) {
    if (scale) return softfloat::mul<W>(a, b, mode, scale);
    Rounding rounding(mode);
    volatile double x = value<64>(a), y = value<64>(b), result = x * y;
    return canonical<64>(encoding<64>(result));
  } else {
    if constexpr (W == 32) {
      if (!scale) {
        Rounding rounding(mode);
        volatile float x = value<32>(a), y = value<32>(b), result = x * y;
        return canonical<32>(encoding<32>(result));
      }
    }
    if (scale < -512 || scale > 512) return softfloat::mul<W>(a, b, mode, scale);
    Rounding rounding(RN);
    volatile double x = W == 16 ? half(a) : double(value<32>(a));
    volatile double y = W == 16 ? half(b) : double(value<32>(b));
    volatile double product = x * y;
    volatile double scaled = std::ldexp(product, scale);
    const u64 bits = canonical<64>(encoding<64>(scaled));
    if constexpr (W == 16) return softfloat::convert<64, 16>(bits, mode);
    else {
      if (softfloat::isnan<64>(bits)) return softfloat::nan<32>();
      Rounding narrowing(mode);
      volatile float result = float(scaled);
      return encoding<32>(result);
    }
  }
}

template <int W> SASS_HD inline u64 fma(u64 a, u64 b, u64 c, Rnd mode) {
  if constexpr (W == 16) {
    const auto x = softfloat::unpack<16>(a), y = softfloat::unpack<16>(b), z = softfloat::unpack<16>(c);
    const int sign = x.term.sign ^ y.term.sign;
    if (x.kind == softfloat::NaN || y.kind == softfloat::NaN || z.kind == softfloat::NaN)
      return softfloat::nan<16>();
    if ((x.kind == softfloat::Inf && y.kind == softfloat::Zero) ||
        (y.kind == softfloat::Inf && x.kind == softfloat::Zero)) return softfloat::nan<16>();
    if (x.kind == softfloat::Inf || y.kind == softfloat::Inf)
      return z.kind == softfloat::Inf && z.term.sign != sign ? softfloat::nan<16>() : softfloat::inf<16>(sign);
    if (z.kind == softfloat::Inf) return c;
    Rounding rounding(RN);
    volatile double av = half(a), bv = half(b), product = av * bv;
    const auto sum = round_odd_sum(product, half(c));
    if (sum.sum == 0 && sum.residual == 0) {
      const bool zero_product = x.kind == softfloat::Zero || y.kind == softfloat::Zero;
      return u64(zero_product && z.kind == softfloat::Zero && z.term.sign == sign ? sign : mode == RM) << 15;
    }
    return softfloat::convert<64, 16>(encoding<64>(sum.odd), mode);
  } else {
    Rounding rounding(mode);
    volatile Native<W> x = value<W>(a), y = value<W>(b), z = value<W>(c);
    volatile Native<W> result = std::fma(x, y, z);
    return canonical<W>(encoding<W>(result));
  }
}

template <int S, int D> SASS_HD inline u64 convert(u64 bits, Rnd mode) {
  if constexpr (S == 16 || D == 16 || S == D) return softfloat::convert<S, D>(bits, mode);
  else {
    if (softfloat::isnan<S>(bits)) return (bits >> (S - 1)) << (D - 1) | softfloat::nan<D>();
    Rounding rounding(mode);
    volatile Native<S> source = value<S>(bits);
    volatile Native<D> result = Native<D>(source);
    return encoding<D>(result);
  }
}

template <int W> SASS_HD inline bool isnan(u64 bits) { return softfloat::isnan<W>(bits); }
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
