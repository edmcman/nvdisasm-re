#pragma once
#include <algorithm>
#include <bit>
#include <cstdint>
#include <map>
#include <memory>
#include <numeric>
#include <vector>
#include "sass/fault.hpp"

namespace sass {
struct Coordination {
  struct Contribution { uint32_t participant, predicate; };
  struct Generation {
    std::shared_ptr<char> owner;
    uint32_t id, count, reduction, result = 0;
    bool implicit, ready = false;
    std::vector<Contribution> arrivals;
  };
  using GenerationPtr = std::shared_ptr<Generation>;
  struct Ticket { GenerationPtr generation; uint32_t participant; };
  struct WarpGeneration {
    std::shared_ptr<char> owner;
    uint32_t participant, mask;
    bool ready = false;
  };
  using WarpTicket = std::shared_ptr<WarpGeneration>;
  struct ConvergenceGeneration {
    std::shared_ptr<char> owner;
    uint32_t warp, id, members, entered = 0, arrived = 0;
    uint64_t target;
    bool ready = false;
  };
  using ConvergencePtr = std::shared_ptr<ConvergenceGeneration>;
  struct ConvergenceTicket { ConvergencePtr generation; uint32_t participant; };
  struct Participant {
    bool live = true;
    uint32_t result = 0;
    GenerationPtr bound, deferred;
  };
  std::shared_ptr<char> identity;
  std::vector<Participant> participants;
  std::vector<WarpTicket> warp_waits;
  uint32_t warp_pending = 0;
  std::map<uint32_t, GenerationPtr> barriers;
  std::map<uint32_t, ConvergencePtr> convergences;

  void reset(uint32_t count) {
    identity = std::make_shared<char>();
    participants.assign(count, {});
    warp_waits.clear();
    warp_pending = 0;
    barriers.clear();
    convergences.clear();
  }
  Participant &participant(uint32_t index) {
    if (index >= participants.size()) throw Fault{"invalid barrier participant"};
    return participants[index];
  }
  void check(const Ticket &ticket, uint32_t index) {
    participant(index);
    if (!ticket.generation || ticket.generation->owner != identity || ticket.participant != index)
      throw Fault{"barrier ticket belongs to another coordinator or participant"};
  }
  bool complete(const GenerationPtr &generation) {
    if (generation->ready) return false;
    uint32_t required = generation->count;
    if (generation->implicit)
      for (uint32_t index = 0; index < participants.size(); ++index)
        if (!participants[index].live && std::none_of(generation->arrivals.begin(), generation->arrivals.end(),
              [&](const Contribution &arrival) { return arrival.participant == index; })) --required;
    if (generation->arrivals.size() != required) return false;
    const uint32_t sum = std::accumulate(generation->arrivals.begin(), generation->arrivals.end(), uint32_t(0),
      [](uint32_t value, const Contribution &arrival) { return value + arrival.predicate; });
    switch (generation->reduction) {
      case 1: generation->result = -(sum == required); break;
      case 2: generation->result = -(sum != 0); break;
      default: generation->result = sum;
    }
    generation->ready = true;
    const uint32_t last_warp = generation->arrivals.back().participant / 32;
    for (const auto &arrival : generation->arrivals) {
      auto &member = participant(arrival.participant);
      if (member.bound == generation || arrival.participant / 32 == last_warp)
        member.result = generation->result;
      if (member.bound == generation) member.bound.reset();
      if (member.deferred == generation) member.deferred.reset();
    }
    barriers.erase(generation->id);
    return true;
  }
  Ticket arrive(uint32_t index, uint32_t id, uint32_t count, bool implicit, uint32_t predicate, uint32_t reduction) {
    if (!participant(index).live || id >= 16 || !count || count > participants.size() ||
        (!implicit && count % 32)) throw Fault{"unsupported barrier ID or participant count"};
    if (predicate > 1 || reduction > 3) throw Fault{"unsupported barrier reduction"};
    auto &entry = barriers[id];
    if (!entry) entry = std::make_shared<Generation>(Generation{identity, id, count, reduction, 0, implicit});
    auto generation = entry;
    if (generation->count != count || generation->implicit != implicit || generation->reduction != reduction)
      throw Fault{"incompatible barrier participant counts or reductions"};
    if (std::any_of(generation->arrivals.begin(), generation->arrivals.end(),
          [&](const Contribution &arrival) { return arrival.participant == index; }))
      throw Fault{"barrier arrival repeats before completion"};
    generation->arrivals.push_back({index, predicate});
    complete(generation);
    return {generation, index};
  }
  Ticket arrive_implicit(uint32_t index, uint32_t id, uint32_t predicate, uint32_t reduction) {
    return arrive(index, id, uint32_t(participants.size()), true, predicate, reduction);
  }
  Ticket arrive_counted(uint32_t index, uint32_t id, uint32_t count, uint32_t predicate, uint32_t reduction) {
    return arrive(index, id, count, false, predicate, reduction);
  }
  bool wait(const Ticket &ticket, uint32_t index) {
    check(ticket, index);
    auto &member = participant(index);
    if (member.deferred == ticket.generation) member.deferred.reset();
    if (!ticket.generation->ready) member.bound = ticket.generation;
    else if (member.bound == ticket.generation) member.bound.reset();
    return ticket.generation->ready;
  }
  void defer(const Ticket &ticket, uint32_t index) {
    check(ticket, index);
    auto &member = participant(index);
    if (!ticket.generation->ready) member.bound = member.deferred = ticket.generation;
  }
  GenerationPtr deferred(uint32_t index) { return participant(index).deferred; }
  uint32_t eligible_lanes(uint32_t index, uint32_t mask) {
    participant(index);
    const uint32_t begin = index / 32 * 32;
    for (uint32_t bits = mask; bits; bits &= bits - 1) {
      const auto &member = participant(begin + std::countr_zero(bits));
      if (!member.live || (member.bound && !member.bound->ready))
        mask &= ~(1u << std::countr_zero(bits));
    }
    return mask;
  }
  uint32_t result(uint32_t index) { return participant(index).result; }
  void check_warp(const WarpTicket &ticket, uint32_t index) {
    participant(index);
    if (!ticket || ticket->owner != identity || ticket->participant != index)
      throw Fault{"warp synchronization ticket belongs to another coordinator or participant"};
  }
  bool complete_warp(uint32_t index) {
    if (!warp_pending) return false;
    const uint32_t begin = index / 32 * 32, end = std::min(begin + 32, uint32_t(participants.size()));
    uint32_t live = 0, arrived = 0;
    for (uint32_t member = begin; member < end; ++member) {
      if (participants[member].live) live |= 1u << (member - begin);
      if (warp_waits[member]) arrived |= 1u << (member - begin);
    }
    bool changed = false;
    for (uint32_t current = begin; current < end; ++current) {
      auto ticket = warp_waits[current];
      if (!ticket) continue;
      const uint32_t required = ticket->mask & live;
      if (required & ~arrived) continue;
      for (uint32_t members = required; members; members &= members - 1)
        if (warp_waits[begin + std::countr_zero(members)]->mask != ticket->mask)
          throw Fault{"WARPSYNC masks disagree"};
      for (uint32_t members = ticket->mask & arrived; members; members &= members - 1) {
        const uint32_t member = begin + std::countr_zero(members);
        warp_waits[member]->ready = true;
        warp_waits[member].reset();
        --warp_pending;
      }
      arrived &= ~ticket->mask;
      changed = true;
    }
    return changed;
  }
  WarpTicket arrive_warp(uint32_t index, uint32_t mask) {
    auto &member = participant(index);
    if (!member.live) throw Fault{"exited warp synchronization participant"};
    if (!(mask >> (index % 32) & 1)) throw Fault{"WARPSYNC mask omits the lane"};
    if (warp_waits.empty()) warp_waits.resize(participants.size());
    if (warp_waits[index]) throw Fault{"warp synchronization arrival repeats before completion"};
    auto ticket = std::make_shared<WarpGeneration>(WarpGeneration{identity, index, mask});
    warp_waits[index] = ticket;
    ++warp_pending;
    complete_warp(index);
    return ticket;
  }
  void enter_convergence(uint32_t index, uint32_t id, uint64_t target, uint32_t members) {
    const uint32_t warp = index / 32, bit = 1u << (index % 32);
    if (!participant(index).live || id >= 16 || !(members & bit))
      throw Fault{"invalid convergence ID or membership"};
    const uint32_t count = std::min(uint32_t(32), uint32_t(participants.size()) - warp * 32);
    if (count < 32 && members >> count) throw Fault{"convergence mask includes absent lanes"};
    auto &entry = convergences[warp * 16 + id];
    if (!entry) entry = std::make_shared<ConvergenceGeneration>(
        ConvergenceGeneration{identity, warp, id, members, 0, 0, target});
    if (entry->members != members || entry->target != target || entry->entered & bit)
      throw Fault{"BSSY reuses an active convergence ID"};
    entry->entered |= bit;
  }
  bool complete_convergence(const ConvergencePtr &generation) {
    if (generation->ready) return false;
    const uint32_t begin = generation->warp * 32;
    const uint32_t end = std::min(begin + 32, uint32_t(participants.size()));
    for (uint32_t index = begin; index < end; ++index)
      if (participants[index].live && (generation->members >> (index - begin) & 1) &&
          !(generation->arrived >> (index - begin) & 1)) return false;
    generation->ready = true;
    convergences.erase(generation->warp * 16 + generation->id);
    return true;
  }
  ConvergenceTicket arrive_convergence(uint32_t index, uint32_t id) {
    if (!participant(index).live) throw Fault{"exited convergence participant"};
    const uint32_t bit = 1u << (index % 32);
    const auto found = convergences.find(index / 32 * 16 + id);
    if (id >= 16 || found == convergences.end() || !(found->second->entered & bit))
      throw Fault{"BSYNC lane has no matching BSSY"};
    auto generation = found->second;
    if (generation->arrived & bit) throw Fault{"convergence arrival repeats before completion"};
    generation->arrived |= bit;
    complete_convergence(generation);
    return {generation, index};
  }
  void check_convergence(const ConvergenceTicket &ticket, uint32_t index) {
    participant(index);
    if (!ticket.generation || ticket.generation->owner != identity || ticket.participant != index)
      throw Fault{"convergence ticket belongs to another coordinator or participant"};
  }
  uint32_t state(uint32_t id) const {
    if (id >= 16) throw Fault{"sixteen barrier states required"};
    const auto found = barriers.find(id);
    if (found == barriers.end()) return 0;
    const auto &generation = *found->second;
    std::vector<uint32_t> warps;
    for (const auto &arrival : generation.arrivals) warps.push_back(arrival.participant / 32);
    std::sort(warps.begin(), warps.end());
    const uint32_t arrived = uint32_t(std::unique(warps.begin(), warps.end()) - warps.begin());
    const uint32_t tag = generation.reduction == 0 ? 8 : generation.reduction == 3 ? 9 : generation.reduction + 9;
    const uint32_t total = std::accumulate(generation.arrivals.begin(), generation.arrivals.end(), uint32_t(0),
      [](uint32_t value, const Contribution &arrival) { return value + arrival.predicate; });
    const uint32_t sum = generation.reduction == 1 ? uint32_t(generation.arrivals.size()) - total : total;
    return tag << 28 | (generation.implicit ? 0 : ((generation.count + 31) / 32) << 20) | sum << 8 | arrived;
  }
  bool exit(uint32_t index) {
    participant(index).live = false;
    bool changed = complete_warp(index);
    for (auto it = barriers.begin(); it != barriers.end();) {
      auto generation = (it++)->second;
      changed |= complete(generation);
    }
    for (auto it = convergences.begin(); it != convergences.end();) {
      auto generation = (it++)->second;
      if (generation->warp == index / 32) changed |= complete_convergence(generation);
    }
    return changed;
  }
};
}
