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
#include "sass/fp.hpp"
#include "sass/async.hpp"
#include "sass/cache.hpp"

namespace sass {
enum Space : int { Ram, Shared, Localmem, Cbank, SPACES };

struct Thread {
  uint32_t r[256], ur[256], sr[256];
  uint8_t p[8], up[8];
  uint64_t pc;
};

// Sparse pages over an optional read-only base image (copied on first write).
struct Memory {
  Memory() = default;
  Memory(const Memory &) = delete;
  Memory &operator=(const Memory &) = delete;
  Memory(Memory &&) = default;
  Memory &operator=(Memory &&) = default;
  static constexpr int PAGE = 8;  // small, since each thread copies a page on its first write to it
  using Page = std::array<uint8_t, 1 << PAGE>;
  std::unordered_map<uint64_t, std::unique_ptr<Page>> pages[SPACES];
  const Memory *base = nullptr;
  Memory *global = nullptr;
  CacheState cache;

  const Page *find(int s, uint64_t n) const {
    if (s == Ram && global) return global->find(s, n);
    if (auto it = pages[s].find(n); it != pages[s].end()) return it->second.get();
    return base ? base->find(s, n) : nullptr;
  }
  Page &own(int s, uint64_t n) {
    if (s == Ram && global) return global->own(s, n);
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

struct OperandGroups {
  uint32_t spans[8], words[8][32][8];
};

struct WarpContext {
  uint32_t present, lane, active, has_waiting, waiting, has_values, has_predicates, pad;
  uint64_t values[32], predicates[32];
  const OperandGroups *groups;
  uint32_t has_bar_result, bar_result;
  AsyncState *async;
  uint32_t write_dependency;
  uint32_t async_ready;
  uint32_t has_bar_states, bar_states[16];
  CacheState *cache;
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

// m: the memory the lane shares (a block's); local: its own localmem (m when alone).
struct Ctx {
  Thread &t; Memory &m, &local; const WarpContext &warp; const Environment &env;
  Memory &space(int s) const { return s == Localmem ? local : m; }
};

inline AsyncState &async(const Ctx &c) {
  if (!c.warp.async) throw Fault{"async-copy state required"};
  return *c.warp.async;
}

inline void complete_async(const Ctx &c) {
  auto &state = async(c);
  for (const auto &copy : state.ready) {
    for (uint32_t byte = 0; byte < copy.size; ++byte)
      c.m.write(Shared, copy.address + byte, uint8_t((byte < 8 ? copy.low : copy.high) >> ((byte % 8) * 8)));
  }
  state.ready.clear();
}

inline void async_issue(const Ctx &c, uint64_t address, uint64_t low, uint64_t high, uint32_t size, bool enabled) {
  if (enabled) async(c).open.push_back({address, low, high, size});
}
inline void async_commit(const Ctx &c) {
  uint32_t board = c.warp.write_dependency;
  if (board >= 6) throw Fault{"async commit needs a write scoreboard"};
  async(c).commit(board);
}
inline void async_wait(const Ctx &c, uint32_t board, uint32_t count, uint32_t mask) {
  if (board < 6) async(c).wait(board, count);
  async(c).waits(mask);
  complete_async(c);
}

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
inline uint64_t bar_result(const Ctx &c) {
  if (!c.warp.has_bar_result) throw Fault{"barrier reduction result required"};
  return c.warp.bar_result;
}
inline uint64_t bar_state(const Ctx &c, uint32_t index) {
  if (!c.warp.has_bar_states || index >= 16) throw Fault{"sixteen barrier states required"};
  return c.warp.bar_states[index];
}
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
inline uint64_t operand_lane(const Ctx &c, uint64_t group, uint64_t source, uint64_t word) {
  const WarpContext &w = warp(c);
  if (!w.groups || group >= 8 || source >= 32 || word >= w.groups->spans[group])
    throw Fault{"warp operand group required or out of range"};
  if (!(w.active >> source & 1)) throw Fault{"warp operand source is inactive"};
  return w.groups->words[group][source][word];
}
inline uint64_t operand_lane(const Ctx &c, uint64_t group, uint64_t source, uint64_t word, uint64_t current) {
  const auto value = operand_lane(c, group, source, word);
  return source == c.warp.lane ? current : value;
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
inline uint64_t mufu(const Ctx &, prim::Mufu fn, bool half, uint64_t x, uint32_t format = 0) {
  uint64_t in[32] = {x};
  return one(0, [&](uint32_t need, uint64_t *out) { return prim::mufu(fn, half, need, in, out, format); });
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
