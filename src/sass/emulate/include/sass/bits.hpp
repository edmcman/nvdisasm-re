// Bitvector helpers for generated code: values of width w live in uint64_t (w <= 64) or u128,
// with every bit above w zero (bitvec's unsigned-modulo-2^w meaning).
#pragma once
#include <cstdint>
#include <type_traits>

namespace sass {
using u64 = uint64_t;
using u128 = unsigned __int128;
using i128 = __int128;

template <int W> using Bits = std::conditional_t<(W <= 64), u64, u128>;
template <int W> using Signed = std::conditional_t<(W <= 64), int64_t, i128>;

template <int W> constexpr Bits<W> mask() { return W == 64 || W == 128 ? ~Bits<W>(0) : (Bits<W>(1) << W) - 1; }
template <int W> constexpr Bits<W> wrap(Bits<W> x) { return x & mask<W>(); }

// x (W bits) as a signed value.
template <int W> constexpr Signed<W> sx(Bits<W> x) {
  constexpr int B = W <= 64 ? 64 : 128;
  return Signed<W>(x << (B - W)) >> (B - W);
}

template <int W, class N> constexpr Bits<W> shl(Bits<W> x, N n) { return n < N(W) ? wrap<W>(x << int(n)) : 0; }
template <int W, class N> constexpr Bits<W> shr(Bits<W> x, N n) { return n < N(W) ? x >> int(n) : 0; }
template <int W, class N> constexpr Bits<W> sar(Bits<W> x, N n) { return wrap<W>(Bits<W>(sx<W>(x) >> (n < N(W) ? int(n) : W - 1))); }
template <int F, int T> constexpr Bits<T> sext(Bits<F> x) { return wrap<T>(Bits<T>(Signed<T>(sx<F>(x)))); }
}  // namespace sass
