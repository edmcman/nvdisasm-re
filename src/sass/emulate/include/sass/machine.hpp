// The machine generated instructions run on: one thread's registers, byte-addressed memory per
// space, and the environment a lane sees (its warp context and the launch's synchronization and
// address-space handlers). The per-lane primitive and synchronization calls reproduce
// sass.semantics.runtime's checks and messages around the shared primitives.
#pragma once
#include <array>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <unordered_map>
#include "sass/bits.hpp"
#include "sass/primitives.hpp"
#include "sass/softfloat.hpp"

namespace sass {
enum Space : int { Ram, Shared, Localmem, Cbank, SPACES };

struct Thread {
  uint32_t r[256], ur[256], sr[256];
  uint8_t p[8], up[8];
  uint64_t pc;
};

// Sparse pages over an optional read-only base image (copied on first write).
struct Memory {
  static constexpr int PAGE = 8;  // small, since each thread copies a page on its first write to it
  using Page = std::array<uint8_t, 1 << PAGE>;
  std::unordered_map<uint64_t, std::unique_ptr<Page>> pages[SPACES];
  const Memory *base = nullptr;

  const Page *find(int s, uint64_t n) const {
    if (auto it = pages[s].find(n); it != pages[s].end()) return it->second.get();
    return base ? base->find(s, n) : nullptr;
  }
  Page &own(int s, uint64_t n) {
    auto &slot = pages[s][n];
    if (!slot) {
      slot = std::make_unique<Page>();
      if (const Page *b = base ? base->find(s, n) : nullptr) *slot = *b; else slot->fill(0);
    }
    return *slot;
  }
  uint8_t read(int s, uint64_t a) const { const Page *p = find(s, a >> PAGE); return p ? (*p)[a & ((1 << PAGE) - 1)] : 0; }
  void write(int s, uint64_t a, uint8_t v) { own(s, a >> PAGE)[a & ((1 << PAGE) - 1)] = v; }

  template <int W> Bits<W> load(int s, uint64_t a) const {
    Bits<W> v = 0;
    for (int i = 0; i < W / 8; ++i) v |= Bits<W>(read(s, a + i)) << (8 * i);
    return v;
  }
  template <int W> void store(int s, uint64_t a, Bits<W> v) {
    for (int i = 0; i < W / 8; ++i) write(s, a + i, uint8_t(v >> (8 * i)));
  }
};

struct WarpContext {
  uint32_t present, lane, active, has_waiting, waiting, has_values, has_predicates, pad;
  uint64_t values[32], predicates[32];
};

enum Sync { Bar, Bssy, Bsync, Break, Warpsync, Bmov };
inline constexpr const char *SYNC_NAMES[] = {"BAR", "BSSY", "BSYNC", "BREAK", "WARPSYNC", "BMOV"};

struct Environment {
  uint32_t sync;                // the synchronization handlers present, a bit per Sync
  uint32_t has_ranges, nranges, nbmov;
  const prim::Range *ranges;    // QSPC's generic address ranges
  const char *ranges_error;     // the runtime's message when the context's ranges are malformed
  uint64_t bmov_keys[16], bmov_values[16];
};

struct Fault { std::string message; };

// m: the memory the lane shares (a cooperative block's); local: its own localmem (m when alone).
struct Ctx {
  Thread &t; Memory &m, &local; const WarpContext &warp; const Environment &env;
  Memory &space(int s) const { return s == Localmem ? local : m; }
};

inline void check(const prim::Status &s) { if (!s) throw Fault{s.message()}; }

// One lane's call of a warp-wide primitive: this lane's result.
template <class F> uint64_t one(uint32_t lane, F f) {
  uint64_t out[32] = {};
  check(f(1u << lane, out));
  return out[lane];
}

inline const WarpContext &warp(const Ctx &c) {
  const WarpContext &w = c.warp;
  if (!w.present) throw Fault{"explicit 32-lane warp context required"};
  if (w.lane >= 32 || !(w.active >> w.lane & 1)) throw Fault{"warp lane is inactive or invalid"};
  return w;
}

// Every lane's `values` (or `predicates`), this lane's being x.
inline std::array<uint64_t, 32> lanes(const Ctx &c, bool predicates, uint64_t x) {
  const WarpContext &w = warp(c);
  if (!(predicates ? w.has_predicates : w.has_values)) throw Fault{predicates ? "warp predicates required" : "warp values required"};
  std::array<uint64_t, 32> v;
  std::memcpy(v.data(), predicates ? w.predicates : w.values, sizeof v);
  v[w.lane] = x;
  return v;
}

inline prim::Warp env_warp(const WarpContext &w) { return {w.active, w.waiting, nullptr, 0}; }

namespace lane {
inline uint64_t lane(const Ctx &c) { return warp(c).lane; }
inline uint64_t active(const Ctx &c) { return warp(c).active; }
inline uint64_t waiting(const Ctx &c) {
  if (!c.warp.present || !c.warp.has_waiting) throw Fault{"warp waiting-lane mask required"};
  return c.warp.waiting;
}
inline uint64_t read_lane(const Ctx &c, uint64_t x, uint64_t src) {
  const auto v = lanes(c, false, x);
  const WarpContext &w = warp(c);
  uint64_t s[32] = {};
  s[w.lane] = src;
  return one(w.lane, [&](uint32_t need, uint64_t *out) { return prim::read_lane(env_warp(w), need, v.data(), s, out); });
}
inline uint64_t ballot(const Ctx &c, uint64_t p) {
  const auto v = lanes(c, true, p);
  const WarpContext &w = warp(c);
  return one(w.lane, [&](uint32_t need, uint64_t *out) { return prim::ballot(env_warp(w), need, v.data(), out); });
}
inline uint64_t match(const Ctx &c, uint64_t x) {
  const auto v = lanes(c, false, x);
  const WarpContext &w = warp(c);
  return one(w.lane, [&](uint32_t need, uint64_t *out) { return prim::match(env_warp(w), need, v.data(), out); });
}
inline uint64_t redux(const Ctx &c, prim::Reduction op, uint64_t x) {
  const auto v = lanes(c, false, x);
  const WarpContext &w = warp(c);
  return one(w.lane, [&](uint32_t need, uint64_t *out) { return prim::redux(op, env_warp(w), need, v.data(), out); });
}
inline uint64_t mufu(const Ctx &, prim::Mufu fn, bool half, uint64_t x) {
  uint64_t in[32] = {x};
  return one(0, [&](uint32_t need, uint64_t *out) { return prim::mufu(fn, half, need, in, out); });
}
inline uint64_t address_space(const Ctx &c, uint64_t address) {
  if (c.env.ranges_error) throw Fault{c.env.ranges_error};
  if (!c.env.has_ranges) throw Fault{"QSPC address_spaces context required"};
  uint64_t in[32] = {address};
  const prim::Warp w{1, 0, c.env.ranges, c.env.nranges};
  return one(0, [&](uint32_t need, uint64_t *out) { return prim::address_space(w, need, in, out); });
}
// A synchronization event (read: BMOV's value for the state encoding, its first input).
inline uint64_t synchronize(const Ctx &c, Sync op, uint64_t first, bool read) {
  if (!read && !first) return 0;
  if (!(c.env.sync >> op & 1)) throw Fault{std::string(SYNC_NAMES[op]) + " synchronization context required"};
  if (!read) return 0;
  for (uint32_t i = 0; i < c.env.nbmov; ++i)
    if (c.env.bmov_keys[i] == first) return c.env.bmov_values[i];
  throw Fault{std::string(SYNC_NAMES[op]) + " reduction/result requires an environment handler"};
}
}  // namespace lane
}  // namespace sass
