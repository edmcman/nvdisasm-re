// Whole-kernel scheduling for native instructions and callback-backed lanes.
// Launch scope interleaves blocks with sequentially consistent global storage.
#pragma once
#include <algorithm>
#include <vector>
#include <map>
#include <numeric>
#include <random>
#include <type_traits>
#include "sass/machine.hpp"
#include "sass/ordered_runtime.hpp"

namespace sass {
struct Operand { uint32_t kind, file, index, span; uint64_t value; };   // kind: 0 none, 1 constant, 2 register
enum File : uint32_t { R, UR, SR, P, UP };

// An instruction's facts the scheduler needs.
struct Info {
  uint32_t exit, collective;
  uint32_t guard_not, ndests;
  Operand guard;
  const char *decode_error;                         // the decoder's message for a word that does not decode
  const uint32_t *dests;                            // trace: file << 16 | index of each register written
  const char *unsupported;
  uint32_t unsupported_scope;
};

using OrderedStep = ordered::Status (*)(Ctx &, ordered::Runtime &, ordered::Frame &);
using OrderedOrdinaryStep = ordered::Status (*)(Ctx &, ordered::Runtime &);
struct Program {
  uint64_t base, count;
  void (*const *code)(Ctx &);
  const Info *info;
  const OrderedStep *ordered_code = nullptr;
  const OrderedOrdinaryStep *ordered_ordinary = nullptr;
};

struct Seed { uint32_t file, index; uint64_t value; };

struct TraceStep { uint64_t thread; uint32_t index, taken, offset, count; };
struct Trace { std::vector<TraceStep> steps; std::vector<uint64_t> values; };

struct Launch {
  uint64_t first, last, block, max_steps, traced;   // [first, last): threads (model 1) or block-aligned threads (model 2)
  const Memory *image;
  const Seed *seeds;
  uint32_t nseeds, block_dims[3], grid_dims[3], tid[3], ctaid[3], laneid, scope, reverse;
  const Environment *env;
  uint64_t count, out_address, out_size, out_stride;   // count: the launch's threads
  uint8_t *outputs;   // (last - first) * out_size bytes
  uint8_t *visited;   // a byte per instruction
  Trace *trace;       // or null
  uint32_t (*step_lane)(uint64_t, Thread *, const WarpContext *, char *, size_t, uint32_t *);
};

enum Outcome : uint32_t { Done, Faulted, Limited, Outside, NoExit, Undecodable, Unmodelled, Omits, Disagree, Stuck };
enum StepOutcome : uint32_t { StepComplete, StepFault, StepSuspended };
struct Result { uint32_t outcome, pad; uint64_t group, thread, pc, steps, a, b; char message[512]; };

inline uint32_t element(const Thread &t, uint32_t file, uint32_t i) {
  switch (file) { case R: return t.r[i]; case UR: return t.ur[i]; case SR: return t.sr[i]; case P: return t.p[i]; default: return t.up[i]; }
}

inline uint64_t read(const Thread &t, const Operand &o) {
  if (o.kind != 2) return o.value;
  const int bits = o.file == P || o.file == UP ? 8 : 32;
  uint64_t v = 0;
  for (uint32_t k = 0; k < o.span; ++k) v |= uint64_t(element(t, o.file, o.index + k)) << (bits * k);
  return v;
}

inline void seed(Thread &t, const Seed &s) {
  const uint32_t v = uint32_t(s.value);
  switch (s.file) { case R: t.r[s.index] = v; break; case UR: t.ur[s.index] = v; break; case SR: t.sr[s.index] = v; break;
                    case P: t.p[s.index] = v & 1; break; default: t.up[s.index] = v & 1; }
}

template<bool NativeResumption> struct Lane {
  Thread t{};
  Memory local;
  uint64_t index = 0, last = 0, steps = 0;
  bool suspended = false, taken = false;
  AsyncState async;
  std::conditional_t<NativeResumption, ordered::Frame, std::nullptr_t> frame{};
  bool live() const { return t.pc != 0; }
};

template<bool NativeRuntime = true, bool Callbacks = true, bool NativeResumption = NativeRuntime> struct Run {
  static_assert(!NativeResumption || NativeRuntime);
  using Lane = sass::Lane<NativeResumption>;
  static constexpr bool Resumption = NativeResumption || Callbacks;
  const Program &p;
  const Launch &l;
  Result &r;
  uint64_t group = 0;
  std::conditional_t<NativeRuntime, std::map<uint64_t, ordered::SchedulerRuntime>, std::nullptr_t> runtimes{};

  uint32_t lane_id(const Lane &u) const { return uint32_t(u.index % l.block % 32); }

  void coordinates(Thread &t, uint64_t rank, const uint32_t *dims, const uint32_t *regs) {
    t.sr[regs[0]] = uint32_t(rank % dims[0]);
    rank /= dims[0];
    t.sr[regs[1]] = uint32_t(rank % dims[1]);
    t.sr[regs[2]] = uint32_t(rank / dims[1]);
  }

  uint32_t stop(Outcome o, uint64_t thread, uint64_t pc, const char *message = "", uint64_t a = 0, uint64_t b = 0) {
    r.outcome = o, r.group = group, r.thread = thread, r.pc = pc, r.a = a, r.b = b;
    std::snprintf(r.message, sizeof r.message, "%s", message);
    return o;
  }

  void start(Lane &u, uint64_t thread) {
    u.index = thread, u.last = p.base, u.t.pc = p.base;
    for (uint32_t i = 0; i < l.nseeds; ++i) seed(u.t, l.seeds[i]);
    const uint64_t local = thread % l.block, block = thread / l.block;
    if (l.block_dims[1] == 1 && l.block_dims[2] == 1 && l.grid_dims[1] == 1 && l.grid_dims[2] == 1) {
      u.t.sr[l.tid[0]] = uint32_t(local), u.t.sr[l.ctaid[0]] = uint32_t(block);
      for (int i = 1; i < 3; ++i) u.t.sr[l.tid[i]] = u.t.sr[l.ctaid[i]] = 0;
    } else {
      coordinates(u.t, local, l.block_dims, l.tid);
      coordinates(u.t, block, l.grid_dims, l.ctaid);
    }
    u.t.sr[l.laneid] = uint32_t(local % 32);
  }

  // The PC check every turn begins with; returns the instruction index or the outcome (>= count).
  bool fetch(uint64_t pc, uint64_t thread, uint64_t &i, uint32_t &outcome) {
    i = (pc - p.base) >> 4;
    if (pc < p.base || i >= p.count || (pc - p.base) & 15) return outcome = stop(Outside, thread, pc), false;
    l.visited[i] = 1;
    if (p.info[i].decode_error) return outcome = stop(Undecodable, thread, pc, p.info[i].decode_error), false;
    return true;
  }

  __attribute__((always_inline)) inline ordered::Status native_step(Lane &u, uint64_t i, Ctx &c, bool &progressed) {
    if constexpr (NativeResumption) if (p.ordered_code && p.ordered_code[i]) {
      auto &runtime = runtimes[group];
      if (!u.suspended) u.frame.reset();
      const auto position = u.frame.position;
      const bool pending = u.frame.pending;
      runtime.bind(c, u.frame, u.index, uint32_t(l.block));
      const auto status = p.ordered_code[i](c, runtime, u.frame);
      progressed |= position != u.frame.position || pending != u.frame.pending;
      return status;
    }
    if constexpr (NativeRuntime) if (p.ordered_ordinary && p.ordered_ordinary[i]) {
      auto &runtime = runtimes[group];
      runtime.bind(c, u.index, uint32_t(l.block));
      return p.ordered_ordinary[i](c, runtime);
    }
    p.code[i](c);
    return ordered::Complete;
  }

  uint32_t step(Lane &u, uint64_t pc, uint64_t i, Memory &shared, WarpContext &w, bool &progressed) {
    w.cache = &(shared.global ? *shared.global : shared).cache;
    if (u.steps >= l.max_steps) return stop(Limited, u.index, pc);
    const Info &info = p.info[i];
    if (!u.suspended) {
      if (Resumption || (l.trace && u.index < l.traced))
        u.taken = !info.guard.kind || (read(u.t, info.guard) ^ info.guard_not);
    }
    w.async = &u.async;
    w.async_ready = uint32_t(u.async.ready.size());
    if (l.scope >= info.unsupported_scope && info.unsupported && (!info.guard.kind || (read(u.t, info.guard) ^ info.guard_not)))
      return stop(Faulted, u.index, pc, info.unsupported);
    const bool traced = l.trace && u.index < l.traced;
    const bool taken = traced && u.taken;
    Environment env = *l.env;
    if (l.scope) env.sync |= (1u << Bar) | (1u << Bssy) | (1u << Bsync) | (1u << Warpsync);
    Ctx c{u.t, shared, u.local, w, env};
    if constexpr (Callbacks) {
      char message[512] = {};
      uint32_t advanced = 0;
      const uint32_t status = l.step_lane(u.index, &u.t, &w, message, sizeof message, &advanced);
      progressed |= bool(advanced);
      if (status == StepFault) return stop(Faulted, u.index, pc, message);
      if (status == StepSuspended) {
        if (u.t.pc != pc) return stop(Faulted, u.index, pc, "suspended instruction changed PC");
        u.suspended = true;
        return Done;
      }
      if (status != StepComplete) return stop(Faulted, u.index, pc, "invalid instruction status");
    } else try {
      if (w.async_ready) complete_async(c);
      const auto status = native_step(u, i, c, progressed);
      if (status == ordered::Faulted) {
        if constexpr (NativeResumption) return stop(Faulted, u.index, pc, u.frame.error.c_str());
        else return stop(Faulted, u.index, pc, "unexpected native runtime fault");
      }
      if (status == ordered::Suspended) {
        if (u.t.pc != pc) return stop(Faulted, u.index, pc, "suspended instruction changed PC");
        u.suspended = true;
        return Done;
      }
    } catch (const Fault &f) { return stop(Faulted, u.index, pc, f.message.c_str()); }
    if constexpr (Resumption) {
      progressed = true;
      u.suspended = false;
    }
    if (w.coordination && !u.live()) w.coordination->exit(uint32_t(u.index % l.block));
    u.last = pc;
    ++u.steps;
    if (traced) {
      l.trace->steps.push_back({u.index, uint32_t(i), taken, uint32_t(l.trace->values.size()), taken ? info.ndests : 0});
      if (taken)
        for (uint32_t k = 0; k < info.ndests; ++k) l.trace->values.push_back(element(u.t, info.dests[k] >> 16, info.dests[k] & 0xffff));
    }
    return Done;
  }

  uint32_t finish(std::vector<Lane *> &lanes, Memory &(*memory)(Lane &, void *), void *arg) {
    for (Lane *u : lanes) {
      if (!p.info[(u->last - p.base) >> 4].exit) return stop(NoExit, u->index, u->last);
      const Memory &m = memory(*u, arg);
      for (uint64_t j = 0; j < l.out_size; ++j)
        l.outputs[(u->index - l.first) * l.out_size + j] = m.read(Ram, l.out_address + u->index * l.out_stride + j);
      r.steps += u->steps;
    }
    return Done;
  }

  // Model 1.
  uint32_t single() {
    WarpContext alone{};   // no warp context: collectives fail as in a isolated thread
    for (uint64_t thread = l.first; thread < l.last; ++thread) {
      group = thread;
      Lane u;
      u.local.base = l.image;
      start(u, thread);
      while (u.live()) {
        uint64_t i; uint32_t o;
        if (!fetch(u.t.pc, thread, i, o)) return o;
        bool progressed = false;
        if (uint32_t s = step(u, u.t.pc, i, u.local, alone, progressed)) return s;
        if (u.suspended && !progressed) return stop(Stuck, thread, u.t.pc);
      }
      std::vector<Lane *> one{&u};
      if (uint32_t s = finish(one, [](Lane &u, void *) -> Memory & { return u.local; }, nullptr)) return s;
    }
    return Done;
  }

  // schedule.turn: Done with moved set when a lane stepped.
  uint32_t turn(std::vector<Lane *> &warp, Coordination &coordination,
                uint32_t block_count, Memory &shared, bool &moved, uint32_t polled = 0) {
    moved = false;
    uint64_t pc = UINT64_MAX;
    if constexpr (Resumption)
      for (Lane *u : warp) if (u->live() && u->suspended && !(polled >> lane_id(*u) & 1)) pc = std::min(pc, u->t.pc);
    if (pc == UINT64_MAX)
      for (Lane *u : warp) if (u->live() && !(polled >> lane_id(*u) & 1)) pc = std::min(pc, u->t.pc);
    if (pc == UINT64_MAX) return Done;
    std::vector<Lane *> active, taking;
    for (Lane *u : warp) if (u->live() && u->t.pc == pc && !(polled >> lane_id(*u) & 1)) active.push_back(u);
    uint64_t i; uint32_t o;
    if (!fetch(pc, active[0]->index, i, o)) return o;
    if constexpr (Resumption) moved = std::any_of(active.begin(), active.end(), [](Lane *u) { return !u->suspended; });
    else moved = true;
    const Info &info = p.info[i];
    for (Lane *u : active) {
      bool taken;
      if constexpr (Resumption) taken = u->suspended ? u->taken : !info.guard.kind || (read(u->t, info.guard) ^ info.guard_not);
      else taken = !info.guard.kind || (read(u->t, info.guard) ^ info.guard_not);
      if (taken) taking.push_back(u);
    }
    if (l.scope >= info.unsupported_scope && info.unsupported && !taking.empty())
      return stop(Faulted, taking.front()->index, pc, info.unsupported);
    WarpContext w{};
    w.coordination = &coordination;
    if (info.collective) {
      w.present = 1, w.has_waiting = 1;
      for (Lane *u : taking) w.active |= 1u << lane_id(*u);
      for (Lane *u : warp)
        if (u->live() && std::find(active.begin(), active.end(), u) == active.end()) w.waiting |= 1u << lane_id(*u);
    }
    for (Lane *u : active) {
      w.lane = lane_id(*u);
      if (uint32_t s = step(*u, pc, i, shared, w, moved)) return s;
    }
    if constexpr (Resumption)
      if (!moved && !active.empty()) {
        for (Lane *u : active) polled |= 1u << lane_id(*u);
        return turn(warp, coordination, block_count, shared, moved, polled);
      }
    return Done;
  }

  uint32_t blocks() {
    struct Block {
      Memory memory;
      std::vector<Lane> lanes;
      std::vector<std::vector<Lane *>> warps;
      Coordination coordination;
    };
    Memory global; global.base = l.image;
    const uint64_t n = (l.last - l.first + l.block - 1) / l.block;
    std::vector<Block> blocks(n);
    std::mt19937 random(l.reverse > 1 ? l.reverse - 2 : 0);
    const auto order = [&](size_t count) {
      std::vector<size_t> indices(count);
      std::iota(indices.begin(), indices.end(), 0);
      if (l.reverse == 1) std::reverse(indices.begin(), indices.end());
      else if (l.reverse > 1) std::shuffle(indices.begin(), indices.end(), random);
      return indices;
    };
    for (uint64_t j = 0; j < n; ++j) {
      auto &b = blocks[j];
      b.memory.base = l.image;
      b.memory.global = l.scope == 2 ? &global : nullptr;
      const uint64_t first = l.first + j * l.block;
      b.lanes.resize(std::min(first + l.block, l.last) - first);
      b.coordination.reset(uint32_t(b.lanes.size()));
      for (uint64_t k = 0; k < b.lanes.size(); ++k) {
        start(b.lanes[k], first + k);
        b.lanes[k].local.base = l.image;
        if (k % 32 == 0) b.warps.emplace_back();
        b.warps.back().push_back(&b.lanes[k]);
      }
    }
    for (;;) {
      bool any = false, alive = false;
      for (size_t j : order(n)) {
        auto &b = blocks[j];
        group = b.lanes.front().index;
        std::vector<Lane *> live;
        for (auto &u : b.lanes) if (u.live()) live.push_back(&u);
        if (live.empty()) continue;
        alive = true;
        for (size_t index : order(b.warps.size())) {
          bool moved;
          if (uint32_t s = turn(b.warps[index], b.coordination,
                                uint32_t(b.lanes.size()), b.memory, moved)) return s;
          any |= moved;
        }
        if (l.scope == 1) break;
      }
      if (!alive) break;
      if (!any) return stop(Stuck, group, 0);
    }
    for (auto &b : blocks) {
      std::vector<Lane *> all;
      for (auto &u : b.lanes) all.push_back(&u);
      if (uint32_t s = finish(all, [](Lane &, void *m) -> Memory & { return *static_cast<Memory *>(m); }, &b.memory)) return s;
    }
    return Done;
  }

};

template<bool NativeRuntime = true, bool Callbacks = true, bool NativeResumption = NativeRuntime>
inline uint32_t run(const Program &p, const Launch &l, Result &r) {
  if constexpr (Callbacks)
    if (!l.step_lane) return sass::run<NativeRuntime, false, NativeResumption>(p, l, r);
  r = Result{};
  Run<NativeRuntime, Callbacks, NativeResumption> run{p, l, r};
  return l.scope ? run.blocks() : run.single();
}
}  // namespace sass

extern "C" {
size_t sass_trace_size(const sass::Trace *t) { return t->steps.size(); }
void sass_trace_step(const sass::Trace *t, size_t k, uint64_t *thread, uint32_t *index, uint32_t *taken, const uint64_t **values, uint32_t *count) {
  const auto &s = t->steps[k];
  *thread = s.thread, *index = s.index, *taken = s.taken, *values = t->values.data() + s.offset, *count = s.count;
}
sass::Trace *sass_trace_new() { return new sass::Trace; }
void sass_trace_free(sass::Trace *t) { delete t; }
}
