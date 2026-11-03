// Whole-kernel scheduling for native instructions and callback-backed lanes.
// Launch scope interleaves blocks with sequentially consistent global storage.
#pragma once
#include <algorithm>
#include <vector>
#include "sass/machine.hpp"

namespace sass {
struct Operand { uint32_t kind, file, index, span; uint64_t value; };   // kind: 0 none, 1 constant, 2 register
enum File : uint32_t { R, UR, SR, P, UP };

// An instruction's facts the scheduler needs (from schedule.operand_location and sync_form).
struct Info {
  uint32_t exit, bar_sync, warpsync, collective;   // collective: 0 none, 1 values, 2 predicates
  uint32_t guard_not, pp_not, sync_kind, ndests;   // sync_kind: 0 unmodelled, 1 all, 2 immediate, 3 operand
  Operand guard, pp, source, sync;
  uint64_t sync_mask;
  const char *decode_error;                         // the decoder's message for a word that does not decode
  const uint32_t *dests;                            // trace: file << 16 | index of each register written
  const char *unsupported;
};

struct Program { uint64_t base, count; void (*const *code)(Ctx &); const Info *info; };

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
  uint32_t (*step_lane)(uint64_t, Thread *, const WarpContext *, char *, size_t);
};

enum Outcome : uint32_t { Done, Faulted, Limited, Outside, NoExit, Undecodable, Unmodelled, Omits, Disagree, Stuck };
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

struct Lane {
  Thread t{};
  Memory local;
  uint64_t index = 0, last = 0, steps = 0;
  bool waiting = false, released = false, has_sync = false;
  uint32_t sync = 0;
  bool live() const { return t.pc != 0; }
};

struct Run {
  const Program &p;
  const Launch &l;
  Result &r;
  uint64_t group = 0;

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

  uint32_t step(Lane &u, uint64_t pc, uint64_t i, Memory &shared, const WarpContext &w) {
    if (u.steps >= l.max_steps) return stop(Limited, u.index, pc);
    u.last = pc;
    const Info &info = p.info[i];
    if (l.scope == 2 && info.unsupported && (!info.guard.kind || (read(u.t, info.guard) ^ info.guard_not)))
      return stop(Faulted, u.index, pc, info.unsupported);
    const bool traced = l.trace && u.index < l.traced;
    const bool taken = traced && (!info.guard.kind || (read(u.t, info.guard) ^ info.guard_not));
    Ctx c{u.t, shared, u.local, w, *l.env};
    if (l.step_lane) {
      char message[512] = {};
      if (l.step_lane(u.index, &u.t, &w, message, sizeof message)) return stop(Faulted, u.index, pc, message);
    } else try { p.code[i](c); } catch (const Fault &f) { return stop(Faulted, u.index, pc, f.message.c_str()); }
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
    static const WarpContext alone{};   // no warp context: collectives fail as in a isolated thread
    for (uint64_t thread = l.first; thread < l.last; ++thread) {
      group = thread;
      Lane u;
      u.local.base = l.image;
      start(u, thread);
      while (u.live()) {
        uint64_t i; uint32_t o;
        if (!fetch(u.t.pc, thread, i, o)) return o;
        if (uint32_t s = step(u, u.t.pc, i, u.local, alone)) return s;
      }
      std::vector<Lane *> one{&u};
      if (uint32_t s = finish(one, [](Lane &u, void *) -> Memory & { return u.local; }, nullptr)) return s;
    }
    return Done;
  }

  // Model 2: schedule.release_warpsyncs.
  uint32_t release(std::vector<Lane *> &warp) {
    for (Lane *t : warp) {
      if (!t->has_sync) continue;
      std::vector<Lane *> members;
      for (Lane *u : warp) if (t->sync >> lane_id(*u) & 1 && u->live()) members.push_back(u);
      if (std::any_of(members.begin(), members.end(), [](Lane *u) { return !u->has_sync; })) continue;
      for (Lane *u : members) if (u->sync != t->sync) return stop(Disagree, t->index, 0, "", t->sync, u->sync);
      for (Lane *u : members) u->waiting = false, u->released = true, u->has_sync = false;
    }
    return Done;
  }

  // schedule.turn: Done with moved set when a lane stepped.
  uint32_t turn(std::vector<Lane *> &warp, Memory &shared, bool &moved) {
    moved = false;
    if (uint32_t s = release(warp)) return s;
    uint64_t pc = UINT64_MAX;
    for (Lane *u : warp) if (u->live() && !u->waiting) pc = std::min(pc, u->t.pc);
    if (pc == UINT64_MAX) return Done;
    std::vector<Lane *> active, taking;
    for (Lane *u : warp) if (u->live() && !u->waiting && u->t.pc == pc) active.push_back(u);
    uint64_t i; uint32_t o;
    if (!fetch(pc, active[0]->index, i, o)) return o;
    moved = true;
    const Info &info = p.info[i];
    for (Lane *u : active) if (!info.guard.kind || (read(u->t, info.guard) ^ info.guard_not)) taking.push_back(u);
    if (l.scope == 2 && info.unsupported && !taking.empty())
      return stop(Faulted, taking.front()->index, pc, info.unsupported);
    const auto drop = [&](const std::vector<Lane *> &held) {
      active.erase(std::remove_if(active.begin(), active.end(), [&](Lane *u) { return std::find(held.begin(), held.end(), u) != held.end(); }), active.end());
    };
    WarpContext w{};
    if (info.bar_sync && !std::all_of(taking.begin(), taking.end(), [](Lane *u) { return u->released; })) {
      for (Lane *u : taking) u->waiting = true;
      drop(taking);
    } else if (info.warpsync) {
      std::vector<Lane *> held;
      for (Lane *u : taking) if (!u->released && (read(u->t, info.pp) ^ info.pp_not)) held.push_back(u);
      for (Lane *u : held) {
        uint64_t mask = 0;
        switch (info.sync_kind) {
          case 0: return stop(Unmodelled, u->index, pc);
          case 1: mask = 0xffffffff; break;
          case 2: mask = info.sync_mask; break;
          default: mask = read(u->t, info.sync) & 0xffffffff;
        }
        if (!(mask >> lane_id(*u) & 1)) return stop(Omits, u->index, pc, "", mask);
        u->waiting = true, u->has_sync = true, u->sync = uint32_t(mask);
      }
      drop(held);
    } else if (info.collective) {
      w.present = 1, w.has_waiting = 1;
      (info.collective == 1 ? w.has_values : w.has_predicates) = 1;
      uint64_t *values = info.collective == 1 ? w.values : w.predicates;
      for (Lane *u : taking) values[lane_id(*u)] = read(u->t, info.source), w.active |= 1u << lane_id(*u);
      for (Lane *u : warp)
        if (u->live() && std::find(active.begin(), active.end(), u) == active.end()) w.waiting |= 1u << lane_id(*u);
    }
    for (Lane *u : active) {
      WarpContext mine = w;
      mine.lane = lane_id(*u);
      if (uint32_t s = step(*u, pc, i, shared, info.collective ? mine : w)) return s;
      u->released = false;
    }
    return Done;
  }

  uint32_t blocks() {
    struct Block {
      Memory memory;
      std::vector<Lane> lanes;
      std::vector<std::vector<Lane *>> warps;
    };
    Memory global; global.base = l.image;
    const uint64_t n = (l.last - l.first + l.block - 1) / l.block;
    std::vector<Block> blocks(n);
    for (uint64_t j = 0; j < n; ++j) {
      auto &b = blocks[j];
      b.memory.base = l.image;
      b.memory.global = l.scope == 2 ? &global : nullptr;
      const uint64_t first = l.first + j * l.block;
      b.lanes.resize(std::min(first + l.block, l.last) - first);
      for (uint64_t k = 0; k < b.lanes.size(); ++k) {
        start(b.lanes[k], first + k);
        b.lanes[k].local.base = l.image;
        if (k % 32 == 0) b.warps.emplace_back();
        b.warps.back().push_back(&b.lanes[k]);
      }
    }
    for (;;) {
      bool any = false, alive = false;
      for (uint64_t j = 0; j < n; ++j) {
        auto &b = blocks[l.reverse ? n - 1 - j : j];
        group = b.lanes.front().index;
        std::vector<Lane *> live;
        for (auto &u : b.lanes) if (u.live()) live.push_back(&u);
        if (live.empty()) continue;
        alive = true;
        if (std::all_of(live.begin(), live.end(), [](Lane *u) { return u->waiting && !u->has_sync; }))
          for (Lane *u : live) u->waiting = false, u->released = true;
        for (size_t k = 0; k < b.warps.size(); ++k) {
          bool moved;
          if (uint32_t s = turn(b.warps[l.reverse ? b.warps.size() - 1 - k : k], b.memory, moved)) return s;
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

inline uint32_t run(const Program &p, const Launch &l, Result &r) {
  r = Result{};
  Run run{p, l, r};
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
