// Device intrinsics require -ftz=false; --use_fast_math is incompatible.
#pragma once
#include "sass/fp_convert.hpp"
#include "sass/fp_multiply.hpp"
#ifdef __CUDACC__
#include <cuda_fp16.h>
#endif
#ifdef __CUDA_ARCH__
namespace sass::fp_cuda {
using namespace fp_common;
template <int W> SASS_HD inline u64 canonical(u64 bits) {
  return is_nan<W>(bits) ? nan_bits<W>() : bits;
}
SASS_HD inline float narrow(double x, Rnd mode) {
  switch (mode) {
    case RZ: return __double2float_rz(x);
    case RP: return __double2float_ru(x);
    case RM: return __double2float_rd(x);
    default: return __double2float_rn(x);
  }
}
SASS_HD inline float half_float(u64 bits) {
  return __half2float(__ushort_as_half(uint16_t(bits)));
}
SASS_HD inline u64 half_from_float(float x, Rnd mode) {
  switch (mode) {
    case RZ: return __half_as_ushort(__float2half_rz(x));
    case RP: return __half_as_ushort(__float2half_ru(x));
    case RM: return __half_as_ushort(__float2half_rd(x));
    default: return __half_as_ushort(__float2half_rn(x));
  }
}
SASS_HD inline u64 half_from_double(double x, Rnd mode) {
  if (mode == RN) return __half_as_ushort(__double2half(x));
  // Same-direction narrowing composes exactly; RN instead uses the direct conversion.
  return half_from_float(narrow(x, mode), mode);
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
  if constexpr (W == 16) {
    if (mode == RN)
      return canonical<16>(__half_as_ushort(__hadd_rn(__ushort_as_half(a), __ushort_as_half(b))));
    // Composing two roundings in the same directed mode gives the final directed result.
    return canonical<16>(half_from_float(native_add<32>(half_float(a), half_float(b), mode), mode));
  }
  else return canonical<W>(encoding<W>(native_add<W>(value<W>(a), value<W>(b), mode)));
}
template <int W> SASS_HD inline u64 mul(u64 a, u64 b, Rnd mode, int scale) {
  if constexpr (W == 16) {
    if (!scale) {
      if (mode == RN)
        return canonical<16>(__half_as_ushort(__hmul_rn(__ushort_as_half(a), __ushort_as_half(b))));
      // Half products have at most 22 significant bits and are exact in normal FP32.
      return canonical<16>(half_from_float(__fmul_rn(half_float(a), half_float(b)), mode));
    }
    const u64 product = canonical<32>(encoding<32>(__fmul_rn(half_float(a), half_float(b))));
    return fp_convert::to_half<32>(product, mode, scale);
  }
  else {
    u64 scaled_a = a, scaled_b = b;
    if (scale && !fp_convert::scale_normal<W>(a, scale, scaled_a)
              && !fp_convert::scale_normal<W>(b, scale, scaled_b)) {
      const auto operands = fp_multiply::prepare<W>(a, b, mode, scale);
      if (!operands.multiply) return operands.result;
      scaled_a = operands.a;
      scaled_b = operands.b;
    }
    return canonical<W>(encoding<W>(native_mul<W>(value<W>(scaled_a), value<W>(scaled_b), mode)));
  }
}
template <int W> SASS_HD inline u64 fma(u64 a, u64 b, u64 c, Rnd mode) {
  if constexpr (W == 16) {
    if (mode == RN)
      return canonical<16>(__half_as_ushort(__hfma(__ushort_as_half(a), __ushort_as_half(b), __ushort_as_half(c))));
    // A half FMA's exact finite range fits inside normal FP32; directed composition is exact.
    return canonical<16>(half_from_float(native_fma<32>(half_float(a), half_float(b), half_float(c), mode), mode));
  }
  else return canonical<W>(encoding<W>(native_fma<W>(value<W>(a), value<W>(b), value<W>(c), mode)));
}
template <int S, int D> SASS_HD inline u64 convert(u64 bits, Rnd mode) {
  if constexpr (S == D) return fp_convert::identity<S>(bits);
  else if constexpr (S == 16) {
    if (is_nan<16>(bits)) return (bits >> 15) << (D - 1) | nan_bits<D>();
    if constexpr (D == 32) return encoding<32>(half_float(bits));
    else return encoding<64>(double(half_float(bits)));
  }
  else if constexpr (D == 16) {
    if (is_nan<S>(bits)) return (bits >> (S - 1)) << 15 | 0x7e00;
    if constexpr (S == 32) return half_from_float(value<32>(bits), mode);
    else return half_from_double(value<64>(bits), mode);
  }
  else {
    if (is_nan<S>(bits)) return (bits >> (S - 1)) << (D - 1) | nan_bits<D>();
    if constexpr (S == 32) return encoding<64>(double(value<32>(bits)));
    else return encoding<32>(narrow(value<64>(bits), mode));
  }
}
template <int W> SASS_HD inline bool isnan(u64 bits) { return is_nan<W>(bits); }
template <int W> SASS_HD inline auto comparison_value(u64 bits) {
  if constexpr (W == 16) return half_float(bits);
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
