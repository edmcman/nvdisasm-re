#pragma once
#include "sass/ordered.hpp"
#include <functional>
#include <set>
#include <string_view>
#include <tuple>

namespace sass::ordered {
struct SchedulerRuntime : Runtime {
  template<class V> struct SnapshotValues {
    using Value = V;
    uint32_t mask = 0, arrived = 0, signature = 0;
    const char *type = "warp_snapshot";
    std::array<V, 32> values{};
  };
  using Snapshot = SnapshotValues<uint64_t>;
  using WordSnapshot = SnapshotValues<u128>;
  struct Barrier { uint32_t count = 0, sum = 0; std::set<uint64_t> arrivals; };
  using SnapshotPtr = std::shared_ptr<Snapshot>;
  using BarrierPtr = std::shared_ptr<Barrier>;
  using Poll = std::function<Reply()>;
  std::map<std::tuple<uint64_t, uint64_t, size_t>, SnapshotPtr> captures;
  std::map<std::tuple<uint64_t, uint64_t, size_t>, std::shared_ptr<WordSnapshot>> word_captures;
  std::map<uint32_t, BarrierPtr> barriers;
  Ctx *context = nullptr;
  Frame *frame = nullptr;
  uint64_t participant = 0, warp = 0;
  uint32_t block = 0;
  bool standalone = false;

  Coordination &coordination() {
    if (!context || !context->warp.coordination) throw Fault{"cooperative barrier context required"};
    return *context->warp.coordination;
  }
  uint32_t block_participant() const {
    if (!block) throw Fault{"cooperative barrier context required"};
    return uint32_t(participant % block);
  }
  bool has_coordination() const { return context && context->warp.coordination; }
  bool cooperative_context() override { return has_coordination(); }
  uint32_t barrier_eligible_lanes(uint32_t mask) override {
    return has_coordination() ? coordination().eligible_lanes(block_participant(), mask) : mask;
  }
  uint32_t block_barrier_result() override {
    if (!context) throw Fault{"barrier environment required"};
    return has_coordination() ? coordination().result(block_participant()) : uint32_t(lane::bar_result(*context));
  }
  uint32_t block_barrier_state(uint32_t id) override {
    if (!context) throw Fault{"barrier environment required"};
    return has_coordination() ? coordination().state(id) : uint32_t(lane::bar_state(*context, id));
  }
  void bind(Ctx &c, uint64_t index, uint32_t count, Frame *f = nullptr) {
    context = &c; frame = f; participant = index; block = count; warp = index % count / 32;
  }
  void bind(Ctx &c, Frame &f, uint64_t index, uint32_t count) {
    bind(c, index, count, &f);
  }
  Reply pending(Poll poll) {
    Reply result = poll();
    if (result.status == Suspended) result.ticket = arena.retain("pending", std::move(poll));
    return result;
  }
  Reply block_barrier_wait_deferred() override {
    if (!has_coordination()) return {};
    auto &state = coordination();
    auto generation = state.deferred(block_participant());
    if (!generation) return {};
    state.wait({generation, block_participant()}, block_participant());
    return pending([generation] { return Reply{generation->ready ? Complete : Suspended}; });
  }
  void convergence_enter(uint32_t id, uint64_t target, uint32_t members) override {
    coordination().enter_convergence(block_participant(), id, target, members);
  }
  Reply convergence_wait(uint64_t handle) override {
    auto ticket = arena.object<Coordination::ConvergenceTicket>(handle, "convergence_ticket");
    coordination().check_convergence(ticket, block_participant());
    if (ticket.generation->ready) return {};
    return {Suspended, {}, arena.retain_shared("convergence_pending", std::move(ticket.generation))};
  }
  template<class T> Reply poll_capture(uint64_t ticket, Arena::Object &entry) {
    auto snapshot = static_cast<T *>(entry.value.get());
    if (snapshot->arrived != snapshot->mask) return {Suspended, {}, ticket};
    entry.type = arena.intern(snapshot->type);
    return {Complete, {ticket}};
  }
  template<bool Wide, bool Predicate>
  Reply capture_scalar(uint32_t lane, uint32_t mask, uint64_t value) {
    return capture(captures, lane, mask, Wide ? "warp_snapshot64" : "warp_snapshot",
                   value, 0, "capture", [&](Snapshot &snapshot) {
      if (Predicate ? !context->warp.has_predicates : !context->warp.has_values)
        throw Fault{Predicate ? "warp predicates required" : "warp values required"};
      for (uint32_t index = 0; index < 32; ++index)
        snapshot.values[index] = Predicate ? context->warp.predicates[index] & 1 :
                                 Wide ? context->warp.values[index] : uint32_t(context->warp.values[index]);
    });
  }
  Reply warp_capture(uint32_t lane, uint32_t mask, uint32_t value) override {
    return capture_scalar<false, false>(lane, mask, value);
  }
  Reply warp_capture64(uint32_t lane, uint32_t mask, uint64_t value) override {
    return capture_scalar<true, false>(lane, mask, value);
  }
  Reply warp_capture_predicate(uint32_t lane, uint32_t mask, bool value) override {
    return capture_scalar<false, true>(lane, mask, value);
  }
  template<class T> Reply poll_generation(uint64_t ticket, const char *type) {
    if (!arena.object<T>(ticket, type).ready) return {Suspended, {}, ticket};
    arena.release(ticket);
    return {};
  }
  template<class T, class F>
  Reply capture(std::map<std::tuple<uint64_t, uint64_t, size_t>, std::shared_ptr<T>> &groups,
                uint32_t lane, uint32_t mask, const char *type, typename T::Value value,
                uint32_t signature, const char *pending_type, F fill) {
    if (lane >= 32 || !(mask >> lane & 1)) throw Fault{"capture mask omits the lane"};
    const auto key = std::make_tuple(context->t.pc, warp, frame->position);
    auto &entry = groups[key];
    if (!entry) {
      entry = std::make_shared<T>();
      entry->mask = mask; entry->type = type; entry->signature = signature;
    }
    auto snapshot = entry;
    if (snapshot->mask != mask || std::string_view(snapshot->type) != type ||
        snapshot->signature != signature || snapshot->arrived >> lane & 1)
      throw Fault{"capture contributions disagree or repeat"};
    if (standalone) {
      if (mask & ~context->warp.active) throw Fault{"invalid capture mask"};
      fill(*snapshot);
      snapshot->arrived = mask;
    }
    snapshot->values[lane] = value;
    snapshot->arrived |= 1u << lane;
    if (snapshot->arrived == mask) groups.erase(key);
    if (snapshot->arrived != mask)
      return {Suspended, {}, arena.retain_shared(pending_type, snapshot)};
    return {Complete, {arena.retain_shared(type, snapshot)}};
  }
  Reply poll(uint64_t ticket) override {
    auto *entry = arena.find(ticket);
    if (!entry) throw Fault{"invalid opaque handle"};
    if (entry->type == "capture") return poll_capture<Snapshot>(ticket, *entry);
    if (entry->type == "word_capture") return poll_capture<WordSnapshot>(ticket, *entry);
    if (entry->type == "warp_sync_pending")
      return poll_generation<Coordination::WarpGeneration>(ticket, "warp_sync_pending");
    if (entry->type == "convergence_pending")
      return poll_generation<Coordination::ConvergenceGeneration>(ticket, "convergence_pending");
    Reply result = arena.object<Poll>(ticket, "pending")();
    if (result.status != Suspended) arena.release(ticket);
    else result.ticket = ticket;
    return result;
  }
  uint32_t snapshot_word(uint64_t handle, uint32_t lane, uint32_t word) override {
    auto &snapshot = arena.object<WordSnapshot>(handle, "warp_word_group");
    if (lane >= 32 || !(snapshot.mask >> lane & 1)) throw Fault{"snapshot source is inactive"};
    if (word >= snapshot.signature >> 8) throw Fault{"snapshot word is out of range"};
    return uint32_t(snapshot.values[lane] >> (word * 32));
  }
  uint32_t calculate_snapshot(uint32_t operation, uint64_t handle, uint32_t lane) override {
    if (operation >= std::size(prim::REDUCTION_NAMES) + 3) throw Fault{"unknown snapshot reduction"};
    const auto &snapshot = arena.object<Snapshot>(handle, operation == 2 ? "warp_snapshot64" : "warp_snapshot");
    const bool matching = operation == 1 || operation == 2;
    if (!matching) lane = std::countr_zero(snapshot.mask);
    if (lane >= 32 || !(snapshot.mask >> lane & 1)) throw Fault{"snapshot source is inactive"};
    const prim::Warp warp{snapshot.mask, 0, nullptr, 0};
    prim::Lanes out{};
    switch (operation) {
      case 0: check(prim::ballot(warp, 1u << lane, snapshot.values.data(), out)); break;
      case 1: case 2: check(prim::match(warp, 1u << lane, snapshot.values.data(), out)); break;
      default: check(prim::redux(prim::Reduction(operation - 3), warp, 1u << lane, snapshot.values.data(), out));
    }
    return uint32_t(out[lane]);
  }
  Reply call(const char *operation, std::span<const u128> a) override {
    const std::string_view name(operation);
    const auto arity = [&](size_t count) {
      if (a.size() != count) throw Fault{"runtime argument arity mismatch"};
    };
    if (name == "block_barrier_arrive_implicit" || name == "block_barrier_arrive_counted") {
      const bool implicit = name == "block_barrier_arrive_implicit";
      arity(implicit ? 3 : 4);
      if (!has_coordination()) {
        if (!context) throw Fault{"BAR synchronization context required"};
        lane::synchronize(*context, Bar, 1, false);
        auto generation = std::make_shared<Coordination::Generation>();
        generation->ready = true;
        return {Complete, {arena.retain("block_barrier_ticket", Coordination::Ticket{generation, block_participant()})}};
      }
      auto &state = coordination();
      auto ticket = implicit ? state.arrive_implicit(block_participant(), uint32_t(a[0]), uint32_t(a[1]), uint32_t(a[2])) :
                               state.arrive_counted(block_participant(), uint32_t(a[0]), uint32_t(a[1]), uint32_t(a[2]), uint32_t(a[3]));
      return {Complete, {arena.retain("block_barrier_ticket", std::move(ticket))}};
    }
    if (name == "block_barrier_wait_ready" || name == "block_barrier_defer") {
      arity(1);
      auto ticket = arena.object<Coordination::Ticket>(uint64_t(a[0]), "block_barrier_ticket");
      if (!ticket.generation->owner) return {};
      auto &state = coordination();
      if (name == "block_barrier_defer") { state.defer(ticket, block_participant()); return {}; }
      state.wait(ticket, block_participant());
      return pending([ticket] { return Reply{ticket.generation->ready ? Complete : Suspended}; });
    }
    if (name == "block_barrier_wait_deferred") {
      arity(0);
      return block_barrier_wait_deferred();
    }
    if (name == "block_barrier_release_ticket") {
      arity(1);
      block_barrier_release_ticket(uint64_t(a[0]));
      return {};
    }
    if (name == "block_barrier_result") { arity(0); return {Complete, {block_barrier_result()}}; }
    if (name == "block_barrier_state") { arity(1); return {Complete, {block_barrier_state(uint32_t(a[0]))}}; }
    if (name == "warp_capture" || name == "warp_capture64" || name == "warp_capture_predicate") {
      arity(3);
      const uint32_t lane = uint32_t(a[0]), mask = uint32_t(a[1]);
      const uint64_t value = uint64_t(a[2]);
      if (name == "warp_capture64") return capture_scalar<true, false>(lane, mask, value);
      if (name == "warp_capture_predicate") return capture_scalar<false, true>(lane, mask, value);
      return capture_scalar<false, false>(lane, mask, value);
    }
    if (name == "warp_capture_words1" || name == "warp_capture_words2" || name == "warp_capture_words4") {
      arity(4);
      const uint32_t group = uint32_t(a[0]), lane = uint32_t(a[1]), mask = uint32_t(a[2]);
      const uint32_t count = name.back() - '0';
      if (group >= 8) throw Fault{"warp operand group is out of range"};
      return capture(word_captures, lane, mask, "warp_word_group", a[3], group | count << 8,
                     "word_capture", [&](WordSnapshot &snapshot) {
        const auto *groups = context->warp.groups;
        if (!groups || groups->spans[group] < count) throw Fault{"warp operand group required or too short"};
        for (uint32_t index = 0; index < 32; ++index)
          for (uint32_t word = 0; word < count; ++word)
            snapshot.values[index] |= u128(groups->words[group][index][word]) << (word * 32);
      });
    }
    if (name == "snapshot_word") {
      arity(3);
      return {Complete, {snapshot_word(uint64_t(a[0]), uint32_t(a[1]), uint32_t(a[2]))}};
    }
    if (name == "snapshot_lane" || name == "snapshot_lane64") {
      arity(2);
      auto snapshot = &arena.object<Snapshot>(uint64_t(a[0]), name == "snapshot_lane64" ? "warp_snapshot64" : "warp_snapshot");
      const uint32_t lane = uint32_t(a[1]);
      if (lane >= 32 || !(snapshot->mask >> lane & 1)) throw Fault{"snapshot source is inactive"};
      return {Complete, {snapshot->values[lane]}};
    }
    if (name == "snapshot_release" || name == "snapshot_release64" || name == "snapshot_release_words") {
      arity(1);
      release_object(uint64_t(a[0]), name == "snapshot_release_words" ? "warp_word_group" :
                                   name == "snapshot_release64" ? "warp_snapshot64" : "warp_snapshot");
      return {};
    }
    if (name == "snapshot_ballot" || name == "snapshot_match" || name == "snapshot_match64" ||
        name.starts_with("snapshot_redux_")) {
      const bool matching = name == "snapshot_match" || name == "snapshot_match64";
      arity(matching ? 2 : 1);
      uint32_t operation = name == "snapshot_ballot" ? 0 : name == "snapshot_match" ? 1 : 2;
      if (!matching && name != "snapshot_ballot") {
        operation = 3;
        while (operation < std::size(prim::REDUCTION_NAMES) + 3 &&
               name.substr(15) != prim::REDUCTION_NAMES[operation - 3]) ++operation;
      }
      return {Complete, {calculate_snapshot(operation, uint64_t(a[0]), matching ? uint32_t(a[1]) : 32)}};
    }
    if (name == "barrier_arrive") {
      arity(3);
      const uint32_t id = uint32_t(a[0]), count = uint32_t(a[1]);
      if (id >= 16 || !count || count > block) throw Fault{"invalid barrier"};
      auto &entry = barriers[id];
      if (!entry) entry = std::make_shared<Barrier>(), entry->count = count;
      auto barrier = entry;
      if (barrier->count != count || !barrier->arrivals.insert(participant).second)
        throw Fault{"barrier participant counts disagree or arrival repeats"};
      barrier->sum += uint32_t(a[2]);
      if (barrier->arrivals.size() == count) barriers.erase(id);
      return {Complete, {arena.retain("barrier_ticket", barrier)}};
    }
    if (name == "barrier_wait" || name == "barrier_wait_ready") {
      arity(1);
      auto barrier = arena.object<BarrierPtr>(uint64_t(a[0]), "barrier_ticket");
      return pending([barrier, result = name == "barrier_wait"] {
        if (barrier->arrivals.size() != barrier->count) return Reply{Suspended};
        return Reply{Complete, result ? Values{barrier->sum} : Values{}};
      });
    }
    if (name == "barrier_release_ticket") {
      arity(1);
      barrier_release_ticket(uint64_t(a[0]));
      return {};
    }
    if (name == "warp_sync_arrive" || name == "warp_sync_arrive_exclusive") {
      arity(1);
      Coordination::WarpTicket ticket;
      if (name == "warp_sync_arrive_exclusive" && has_coordination())
        throw Fault{"unsupported exclusive warp synchronization"};
      if (has_coordination()) ticket = coordination().arrive_warp(block_participant(), uint32_t(a[0]));
      else {
        if (!context) throw Fault{"WARPSYNC synchronization context required"};
        lane::synchronize(*context, Warpsync, 1, false);
        ticket = std::make_shared<Coordination::WarpGeneration>();
        ticket->ready = true;
      }
      return {Complete, {arena.retain_shared("warp_sync_ticket", std::move(ticket))}};
    }
    if (name == "warp_sync_wait") {
      arity(1);
      const uint64_t handle = uint64_t(a[0]);
      arena.object<Coordination::WarpGeneration>(handle, "warp_sync_ticket");
      auto ticket = std::static_pointer_cast<Coordination::WarpGeneration>(
          arena.check(handle, "warp_sync_ticket").value);
      if (!ticket->owner) return {};
      coordination().check_warp(ticket, block_participant());
      if (ticket->ready) return {};
      return {Suspended, {}, arena.retain_shared("warp_sync_pending", std::move(ticket))};
    }
    if (name == "warp_sync_release_ticket") {
      arity(1);
      warp_sync_release_ticket(uint64_t(a[0]));
      return {};
    }
    if (name == "cooperative_context") { arity(0); return {Complete, {cooperative_context()}}; }
    if (name == "barrier_eligible_lanes") {
      arity(1); return {Complete, {barrier_eligible_lanes(uint32_t(a[0]))}};
    }
    if (name == "convergence_enter" || name == "convergence_enter_reliable" ||
        name == "convergence_enter_reconvergent") {
      arity(3);
      convergence_enter(uint32_t(a[0]), uint64_t(a[1]), uint32_t(a[2]));
      return {};
    }
    if (name == "convergence_arrive" || name == "convergence_arrive_reliable" ||
        name == "convergence_arrive_reconvergent") {
      arity(1);
      auto ticket = coordination().arrive_convergence(block_participant(), uint32_t(a[0]));
      return {Complete, {arena.retain("convergence_ticket", std::move(ticket))}};
    }
    if (name == "convergence_wait") {
      arity(1);
      return convergence_wait(uint64_t(a[0]));
    }
    if (name == "convergence_release_ticket") {
      arity(1);
      convergence_release_ticket(uint64_t(a[0]));
      return {};
    }
    if (name == "convergence_ack_enter" || name == "convergence_ack_enter_mode" ||
        name == "convergence_ack_wait" || name == "convergence_ack_wait_mode") {
      const bool enter = name == "convergence_ack_enter" || name == "convergence_ack_enter_mode";
      arity((enter ? 2 : 1) + (name.ends_with("_mode") ? 1 : 0));
      if (!context) throw Fault{"convergence acknowledgement context required"};
      lane::synchronize(*context, enter ? Bssy : Bsync, 1, false);
      return {};
    }
    if (name == "convergence_state_read" || name == "convergence_state_read_clear") {
      arity(1);
      if (!context) throw Fault{"convergence state environment required"};
      return {Complete, {lane::synchronize(*context, Bmov, uint64_t(a[0]), true)}};
    }
    if (name == "convergence_break" || name == "convergence_barrier_copy" ||
        name == "convergence_state_import" || name == "convergence_exit_pc_write" ||
        name == "convergence_state_write" || name == "convergence_barrier_write") {
      arity(name == "convergence_break" || name == "convergence_exit_pc_write" ? 1 :
            name == "convergence_barrier_copy" || name == "convergence_state_import" ? 2 : 3);
      if (!context) throw Fault{"convergence state environment required"};
      lane::synchronize(*context, name == "convergence_break" ? Break : Bmov, 1, false);
      return {};
    }
    if (name.ends_with("_ack") && (name.starts_with("barrier_") || name.starts_with("warp_"))) {
      static constexpr std::pair<std::string_view, size_t> acknowledgements[] = {
        {"barrier_packed_arrive_ack", 2}, {"barrier_packed_wait_ack", 2}, {"barrier_packed_defer_ack", 2},
        {"barrier_packed_reduce_ack", 4}, {"barrier_packed_reduce_defer_ack", 4}, {"barrier_scan_ack", 2},
        {"barrier_all_ack", 0}, {"barrier_all_defer_ack", 0}, {"warp_mask_ack", 1},
        {"warp_collective_all_ack", 1}, {"warp_collective_mask_ack", 2},
        {"warp_relative_all_ack", 2}, {"warp_relative_mask_ack", 3}};
      for (const auto &[ack, count] : acknowledgements) if (name == ack) {
        arity(count);
        if (!context) throw Fault{"synchronization context required"};
        lane::synchronize(*context, name.starts_with("warp_") ? Warpsync : Bar, 1, false);
        return {};
      }
    }
    throw Fault{"ordered operation " + std::string(name)};
  }
  void acquire() override {
    if (context->m.atomic_owner) throw Fault{"atomic block already owned"};
    context->m.atomic_owner = &context->t;
  }
  void release() override {
    if (context->m.atomic_owner != &context->t) throw Fault{"atomic block owner mismatch"};
    context->m.atomic_owner = nullptr;
  }
};
}
