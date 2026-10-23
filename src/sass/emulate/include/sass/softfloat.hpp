// Bit-exact binary IEEE arithmetic: the C++ form of sass.semantics.fp.softfloat (round once; NaN
// results are the canonical quiet NaN, which the IR's nanresult replaces by the GPU's).
#pragma once
#include <algorithm>
#include <bit>
#include <cmath>
#include <cstdint>

namespace sass::fp {
using u64 = uint64_t;
using u128 = unsigned __int128;

enum Rnd { RN, RZ, RP, RM };

template <int W> struct Format;
template <> struct Format<16> { static constexpr int E = 5, M = 10; };
template <> struct Format<32> { static constexpr int E = 8, M = 23; };
template <> struct Format<64> { static constexpr int E = 11, M = 52; };

enum Kind { Zero, Finite, Inf, NaN };

// value = (-1)^sign * sig * 2^exp
struct Term { int sign; u128 sig; int exp; };
struct Unpacked { Kind kind; Term term; };

inline int msb(u128 x) {
  u64 hi = u64(x >> 64);
  return hi ? 127 - std::countl_zero(hi) : 63 - std::countl_zero(u64(x));
}

template <int W> constexpr int bias() { return (1 << (Format<W>::E - 1)) - 1; }
template <int W> constexpr u64 exponents() { return (u64(1) << Format<W>::E) - 1; }
template <int W> constexpr u64 nan() { return exponents<W>() << Format<W>::M | u64(1) << (Format<W>::M - 1); }
template <int W> constexpr u64 inf(int sign) { return u64(sign) << (W - 1) | exponents<W>() << Format<W>::M; }

template <int W> Unpacked unpack(u64 bits) {
  constexpr int M = Format<W>::M;
  int sign = bits >> (W - 1) & 1;
  u64 ex = bits >> M & exponents<W>(), mant = bits & ((u64(1) << M) - 1);
  if (ex == exponents<W>()) return {mant ? NaN : Inf, {sign, 0, 0}};
  if (ex == 0) return {mant ? Finite : Zero, {sign, mant, 1 - bias<W>() - M}};
  return {Finite, {sign, u128(u64(1) << M | mant), int(ex) - bias<W>() - M}};
}

// The encoding of a term rounded once in `mode`; zero_sign signs an exact zero.
template <int W> u64 pack(Term t, Rnd mode, int zero_sign) {
  constexpr int M = Format<W>::M;
  if (!t.sig) return u64(zero_sign) << (W - 1);
  const u64 sign = u64(t.sign) << (W - 1);
  const int minimum = 1 - bias<W>(), e = msb(t.sig) + t.exp, shift = std::max(e, minimum) - M, k = shift - t.exp;
  u64 q;
  if (k <= 0) q = u64(t.sig << -k);  // exact: then at most M+1 bits
  else {
    const u128 kept = k >= 128 ? 0 : t.sig >> k, rest = k >= 128 ? t.sig : t.sig & ((u128(1) << k) - 1);
    const bool inexact = rest != 0;
    const int half_cmp = k > 128 ? -1 : k == 128 ? (rest > u128(1) << 127) - (rest < u128(1) << 127)
                                                  : (rest > u128(1) << (k - 1)) - (rest < u128(1) << (k - 1));
    bool up = false;
    switch (mode) {
      case RN: up = half_cmp > 0 || (half_cmp == 0 && (kept & 1)); break;
      case RZ: break;
      case RP: up = inexact && !t.sign; break;
      case RM: up = inexact && t.sign; break;
    }
    q = u64(kept) + up;
  }
  int ex = e;
  if (q >= u64(1) << (M + 1)) q >>= 1, ++ex;
  if (ex > bias<W>()) {
    const bool infinite = mode == RN || (mode == RP && !t.sign) || (mode == RM && t.sign);
    return infinite ? inf<W>(t.sign) : sign | (exponents<W>() - 1) << M | ((u64(1) << M) - 1);
  }
  if (q < u64(1) << M) return sign | q;
  return sign | u64(std::max(ex, minimum) + bias<W>()) << M | (q - (u64(1) << M));
}

// t with its most significant bit at bit 125. Inputs carry at most 106 significant bits, so at
// least 19 low bits are then zero: a jamming shift by fewer bits is exact, and a longer one cancels
// at most one leading bit, far above the jammed bit.
inline Term normal(Term t) {
  const int s = 125 - msb(t.sig);
  return {t.sign, t.sig << s, t.exp - s};
}

inline u128 jam(u128 x, int d) { return d >= 128 ? x != 0 : x >> d | ((x & ((u128(1) << d) - 1)) != 0); }

// a + b, exact unless jammed (then still correctly rounded by pack); a zero sum keeps sign 0.
inline Term sum(Term a, Term b) {
  if (!a.sig) return b;
  if (!b.sig) return a;
  a = normal(a), b = normal(b);
  if (a.exp < b.exp) std::swap(a, b);
  b.sig = jam(b.sig, a.exp - b.exp);
  if (a.sign == b.sign) return {a.sign, a.sig + b.sig, a.exp};
  return a.sig >= b.sig ? Term{a.sign, a.sig - b.sig, a.exp} : Term{b.sign, b.sig - a.sig, a.exp};
}

template <int W> u64 add(u64 a, u64 b, Rnd mode) {
  const auto x = unpack<W>(a), y = unpack<W>(b);
  if (x.kind == NaN || y.kind == NaN) return nan<W>();
  if (x.kind == Inf && y.kind == Inf && x.term.sign != y.term.sign) return nan<W>();
  if (x.kind == Inf) return a;
  if (y.kind == Inf) return b;
  const int zero = x.kind == Zero && y.kind == Zero && x.term.sign == y.term.sign ? x.term.sign : mode == RM;
  return pack<W>(sum(x.term, y.term), mode, zero);
}

inline Term product(Term a, Term b, int scale = 0) { return {a.sign ^ b.sign, a.sig * b.sig, a.exp + b.exp + scale}; }

template <int W> u64 mul(u64 a, u64 b, Rnd mode, int scale) {
  const auto x = unpack<W>(a), y = unpack<W>(b);
  const int sign = x.term.sign ^ y.term.sign;
  if (x.kind == NaN || y.kind == NaN) return nan<W>();
  if ((x.kind == Inf && y.kind == Zero) || (x.kind == Zero && y.kind == Inf)) return nan<W>();
  if (x.kind == Inf || y.kind == Inf) return inf<W>(sign);
  return pack<W>(product(x.term, y.term, scale), mode, sign);
}

template <int W> u64 fma(u64 a, u64 b, u64 c, Rnd mode) {
  const auto x = unpack<W>(a), y = unpack<W>(b), z = unpack<W>(c);
  const int sign = x.term.sign ^ y.term.sign;
  if (x.kind == NaN || y.kind == NaN || z.kind == NaN) return nan<W>();
  if ((x.kind == Inf && y.kind == Zero) || (x.kind == Zero && y.kind == Inf)) return nan<W>();
  const bool infinite = x.kind == Inf || y.kind == Inf;
  if (infinite && z.kind == Inf && z.term.sign != sign) return nan<W>();
  if (infinite) return inf<W>(sign);
  if (z.kind == Inf) return c;
  const bool zero_product = x.kind == Zero || y.kind == Zero;
  const int zero = zero_product && z.kind == Zero && z.term.sign == sign ? sign : mode == RM;
  return pack<W>(sum(product(x.term, y.term), z.term), mode, zero);
}

// An encoding converted between widths (NaN: canonical, keeping the sign).
template <int S, int D> u64 convert(u64 bits, Rnd mode) {
  const auto x = unpack<S>(bits);
  switch (x.kind) {
    case NaN: return u64(x.term.sign) << (D - 1) | nan<D>();
    case Inf: return inf<D>(x.term.sign);
    case Zero: return u64(x.term.sign) << (D - 1);
    default: return pack<D>(x.term, mode, x.term.sign);
  }
}

// Every 16/32/64-bit value is exact in a double, which orders them (NaN unordered, -0 == +0).
template <int W> double value(u64 bits) {
  const auto x = unpack<W>(bits);
  switch (x.kind) {
    case NaN: return NAN;
    case Inf: return x.term.sign ? -INFINITY : INFINITY;
    case Zero: return 0.0;
    default: { const double v = std::ldexp(double(u64(x.term.sig)), x.term.exp); return x.term.sign ? -v : v; }
  }
}

template <int W> bool isnan(u64 bits) { return unpack<W>(bits).kind == NaN; }
template <int W> bool lt(u64 a, u64 b) { return value<W>(a) < value<W>(b); }
template <int W> bool le(u64 a, u64 b) { return value<W>(a) <= value<W>(b); }
template <int W> bool eq(u64 a, u64 b) { return value<W>(a) == value<W>(b); }
}  // namespace sass::fp
