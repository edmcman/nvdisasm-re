// Bitvector helpers for generated code: values of width w live in uint64_t (w <= 64) or u128,
// with every bit above w zero (bitvec's unsigned-modulo-2^w meaning).
#pragma once
#include "sass/fp_common.hpp"
#include <type_traits>

namespace sass {
using u64 = uint64_t;
using u128 = unsigned __int128;
using i128 = __int128;

template <int W> using Bits = std::conditional_t<(W <= 64), u64, u128>;
template <int W> using Signed = std::conditional_t<(W <= 64), int64_t, i128>;

template <int W> SASS_HD constexpr Bits<W> mask() { return W == 64 || W == 128 ? ~Bits<W>(0) : (Bits<W>(1) << W) - 1; }
template <int W> SASS_HD constexpr Bits<W> wrap(Bits<W> x) { return x & mask<W>(); }

// x (W bits) as a signed value.
template <int W> SASS_HD constexpr Signed<W> sx(Bits<W> x) {
  constexpr int B = W <= 64 ? 64 : 128;
  return Signed<W>(x << (B - W)) >> (B - W);
}

template <int W, class N> SASS_HD constexpr Bits<W> shl(Bits<W> x, N n) { return n < N(W) ? wrap<W>(x << int(n)) : 0; }
template <int W, class N> SASS_HD constexpr Bits<W> shr(Bits<W> x, N n) { return n < N(W) ? x >> int(n) : 0; }
template <int W, class N> SASS_HD constexpr Bits<W> sar(Bits<W> x, N n) { return wrap<W>(Bits<W>(sx<W>(x) >> (n < N(W) ? int(n) : W - 1))); }
template <int F, int T> SASS_HD constexpr Bits<T> sext(Bits<F> x) { return wrap<T>(Bits<T>(Signed<T>(sx<F>(x)))); }
template <int W> SASS_HD constexpr Bits<W> lzcount(Bits<W> x) {
  if constexpr (W > 64) {
    u64 hi = u64(x >> 64);
    return hi ? lzcount<64>(hi) - (128 - W) : (W - 64) + lzcount<64>(u64(x));
  } else {
#ifdef __CUDA_ARCH__
    return __clzll(x) - (64 - W);
#else
    return std::countl_zero(u64(x)) - (64 - W);
#endif
  }
}
}  // namespace sass
