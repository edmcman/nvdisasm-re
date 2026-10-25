// The runtime primitives (sass.semantics.primitive) over a whole warp, the one implementation for
// every engine: the generated kernels inline them and sass.semantics.runtime calls them through
// primitives.cpp. Each takes every lane's inputs and fills the result of the lanes in `need` (the
// lanes whose result is used: an unused lane's input may be out of range, as SHFL's lazy ite
// leaves an invalid source lane), returning a Status whose message is the runtime's error.
#pragma once
#include <algorithm>
#include <bit>
#include <cstdint>
#include <cstdio>
#include <string>
#include "sass/mufu.hpp"

namespace sass::prim {
using Lanes = uint64_t[32];

struct Range { uint64_t start, last; uint32_t space; };  // [start, last] of one space tag
struct Warp { uint32_t active, waiting; const Range *ranges; uint32_t nranges; };

struct Status {
  const char *error = nullptr;  // a static message, or `text` when dynamic
  std::string text;
  explicit operator bool() const { return !error && text.empty(); }
  std::string message() const { return error ? error : text; }
};
inline Status ok() { return {}; }
inline Status fail(const char *error) { return {error, {}}; }

enum Space : uint32_t { Global = 1, Shared = 2, Local = 4, SharedCluster = 8 };

template <class F> inline void each(uint32_t mask, F f) {
  for (; mask; mask &= mask - 1) f(std::countr_zero(mask));
}

inline Status read_lane(const Warp &, uint32_t need, const Lanes x, const Lanes src, Lanes out) {
  Status s;
  each(need, [&](int l) { if (src[l] >= 32) s = fail("read_lane source lane out of range"); else out[l] = x[src[l]]; });
  return s;
}

inline Status ballot(const Warp &w, uint32_t need, const Lanes p, Lanes out) {
  uint32_t mask = 0;
  each(w.active, [&](int l) { mask |= uint32_t(p[l] != 0) << l; });
  each(need, [&](int l) { out[l] = mask; });
  return ok();
}

inline Status match(const Warp &w, uint32_t need, const Lanes x, Lanes out) {
  each(need, [&](int l) {
    uint32_t mask = 0;
    each(w.active, [&](int m) { mask |= uint32_t(x[m] == x[l]) << m; });
    out[l] = mask;
  });
  return ok();
}

enum Reduction { And, Or, Xor, Add, Umin, Umax, Smin, Smax };

inline Status redux(Reduction op, const Warp &w, uint32_t need, const Lanes x, Lanes out) {
  if (!w.active) return fail("redux over no active lanes");
  const auto first = std::countr_zero(w.active);
  uint32_t r = uint32_t(x[first]);
  each(w.active & (w.active - 1), [&](int l) {
    const uint32_t v = uint32_t(x[l]);
    switch (op) {
      case And: r &= v; break;
      case Or: r |= v; break;
      case Xor: r ^= v; break;
      case Add: r += v; break;
      case Umin: r = std::min(r, v); break;
      case Umax: r = std::max(r, v); break;
      case Smin: r = uint32_t(std::min(int32_t(r), int32_t(v))); break;
      case Smax: r = uint32_t(std::max(int32_t(r), int32_t(v))); break;
    }
  });
  each(need, [&](int l) { out[l] = r; });
  return ok();
}

// MUFU.fn over F32 lanes (the 64H forms on a double's high word); only SIN/COS are modelled.
enum Mufu { Cos, Sin, Ex2, Lg2, Rcp, Rsq, Rcp64h, Rsq64h, Sqrt, Tanh };
inline constexpr const char *MUFU_NAMES[] = {"COS", "SIN", "EX2", "LG2", "RCP", "RSQ", "RCP64H", "RSQ64H", "SQRT", "TANH"};

inline Status mufu(Mufu fn, bool half, uint32_t need, const Lanes x, Lanes out) {
  if (half || (fn != Sin && fn != Cos))
    return {nullptr, std::string("MUFU approximation is unresolved for ") + MUFU_NAMES[fn] + (half ? " (16-bit)" : "")};
  each(need, [&](int l) { out[l] = mufu::sincos(uint32_t(x[l]), fn == Cos); });
  return ok();
}

// The one-hot space of a generic address among the mapped ranges; CTA shared memory lies inside
// the cluster's shared window, so that pair is unique too.
inline Status address_space(const Warp &w, uint32_t need, const Lanes address, Lanes out) {
  Status s;
  each(need, [&](int l) {
    uint32_t found = 0;
    for (uint32_t i = 0; i < w.nranges; ++i)
      if (w.ranges[i].start <= address[l] && address[l] <= w.ranges[i].last) found |= w.ranges[i].space;
    if (std::popcount(found) != 1 && found != (Shared | SharedCluster)) s = fail("QSPC input address has no unique mapped space");
    else out[l] = found;
  });
  return s;
}

// Bit 59 marks a range-form L2 policy; some of those trap on SM89 and which ones is unknown.
inline Status memory_descriptor(uint64_t descriptor) {
  if (!(descriptor >> 59 & 1)) return ok();
  char text[64];
  std::snprintf(text, sizeof text, "range-form memory descriptor %#llx", (unsigned long long)descriptor);
  return {nullptr, text};
}
}  // namespace sass::prim
