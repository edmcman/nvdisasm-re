// Device intrinsics require -ftz=false; --use_fast_math is incompatible.
#pragma once
#include "sass/softfloat.hpp"
#ifdef __CUDA_ARCH__
namespace sass::fp_cuda {
using namespace fp_common;
template <int W> SASS_HD inline u64 canonical(u64 bits) {
  return softfloat::isnan<W>(bits) ? softfloat::nan<W>() : bits;
}
SASS_HD inline float narrow(double x, Rnd mode) {
  switch (mode) {
    case RZ: return __double2float_rz(x);
    case RP: return __double2float_ru(x);
    case RM: return __double2float_rd(x);
    default: return __double2float_rn(x);
  }
}
template <int W> SASS_HD inline Native<W> native_add(Native<W> a, Native<W> b, Rnd mode) {
  if constexpr (W == 32) {
    switch (mode) {
      case RZ: return __fadd_rz(a, b);
      case RP: return __fadd_ru(a, b);
      case RM: return __fadd_rd(a, b);
      default: return __fadd_rn(a, b);
    }
  }
  else {
    switch (mode) {
      case RZ: return __dadd_rz(a, b);
      case RP: return __dadd_ru(a, b);
      case RM: return __dadd_rd(a, b);
      default: return __dadd_rn(a, b);
    }
  }
}
template <int W> SASS_HD inline Native<W> native_mul(Native<W> a, Native<W> b, Rnd mode) {
  if constexpr (W == 32) {
    switch (mode) {
      case RZ: return __fmul_rz(a, b);
      case RP: return __fmul_ru(a, b);
      case RM: return __fmul_rd(a, b);
      default: return __fmul_rn(a, b);
    }
  }
  else {
    switch (mode) {
      case RZ: return __dmul_rz(a, b);
      case RP: return __dmul_ru(a, b);
      case RM: return __dmul_rd(a, b);
      default: return __dmul_rn(a, b);
    }
  }
}
template <int W> SASS_HD inline Native<W> native_fma(Native<W> a, Native<W> b, Native<W> c, Rnd mode) {
  if constexpr (W == 32) {
    switch (mode) {
      case RZ: return __fmaf_rz(a, b, c);
      case RP: return __fmaf_ru(a, b, c);
      case RM: return __fmaf_rd(a, b, c);
      default: return __fmaf_rn(a, b, c);
    }
  }
  else {
    switch (mode) {
      case RZ: return __fma_rz(a, b, c);
      case RP: return __fma_ru(a, b, c);
      case RM: return __fma_rd(a, b, c);
      default: return __fma_rn(a, b, c);
    }
  }
}

template <int W> SASS_HD inline u64 add(u64 a, u64 b, Rnd mode) {
  if constexpr (W == 16) return softfloat::add<W>(a, b, mode);
  else return canonical<W>(encoding<W>(native_add<W>(value<W>(a), value<W>(b), mode)));
}
template <int W> SASS_HD inline u64 mul(u64 a, u64 b, Rnd mode, int scale) {
  if constexpr (W == 16) return softfloat::mul<W>(a, b, mode, scale);
  else if constexpr (W == 64) {
    if (scale) return softfloat::mul<W>(a, b, mode, scale);
    return canonical<W>(encoding<W>(native_mul<W>(value<W>(a), value<W>(b), mode)));
  } else {
    if (!scale) return canonical<32>(encoding<32>(native_mul<32>(value<32>(a), value<32>(b), mode)));
    if (scale < -512 || scale > 512) return softfloat::mul<W>(a, b, mode, scale);
    const double product = __dmul_rn(double(value<32>(a)), double(value<32>(b)));
    const double scaled = scalbn(product, scale);
    if (softfloat::isnan<64>(encoding<64>(scaled))) return softfloat::nan<32>();
    return encoding<32>(narrow(scaled, mode));
  }
}
template <int W> SASS_HD inline u64 fma(u64 a, u64 b, u64 c, Rnd mode) {
  if constexpr (W == 16) return softfloat::fma<W>(a, b, c, mode);
  else return canonical<W>(encoding<W>(native_fma<W>(value<W>(a), value<W>(b), value<W>(c), mode)));
}
template <int S, int D> SASS_HD inline u64 convert(u64 bits, Rnd mode) {
  if constexpr (S == 16 || D == 16 || S == D) return softfloat::convert<S, D>(bits, mode);
  else {
    if (softfloat::isnan<S>(bits)) return (bits >> (S - 1)) << (D - 1) | softfloat::nan<D>();
    if constexpr (S == 32) return encoding<64>(double(value<32>(bits)));
    else return encoding<32>(narrow(value<64>(bits), mode));
  }
}
template <int W> SASS_HD inline bool isnan(u64 bits) { return softfloat::isnan<W>(bits); }
template <int W> SASS_HD inline auto comparison_value(u64 bits) {
  if constexpr (W == 16) return value<64>(softfloat::convert<16, 64>(bits, RN));
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
}  // namespace sass::fp_cuda
#endif
