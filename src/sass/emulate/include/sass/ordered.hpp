#pragma once
#include "sass/machine.hpp"
#include <deque>
#include <map>
#include <memory>
#include <optional>
#include <span>
#include <string_view>
#include <typeindex>
#include <unordered_map>
#include <utility>
#include <vector>

namespace sass::ordered {
enum Status : uint32_t { Complete, Suspended, Faulted };
struct Values {
  std::array<u128, 2> local{};
  std::vector<u128> extra;
  size_t count = 0;
  Values() = default;
  Values(std::initializer_list<u128> values) : count(values.size()) {
    if (count <= local.size()) std::copy(values.begin(), values.end(), local.begin());
    else extra.assign(values);
  }
  size_t size() const { return count; }
  const u128 &operator[](size_t index) const { return count <= local.size() ? local[index] : extra[index]; }
};
struct Reply {
  Status status = Complete;
  Values values;
  uint64_t ticket = 0;
};
struct Frame {
  size_t position = 0;
  std::vector<u128> values;
  bool pending = false, atomic = false;
  uint64_t ticket = 0;
  Status status = Suspended;
  std::string error;
  void reset() {
    position = 0;
    values.clear();
    pending = atomic = false;
    ticket = 0;
    status = Suspended;
    error.clear();
  }
};
struct Arena {
  struct Object { std::string_view type; std::type_index value_type; std::shared_ptr<void> value; };
  struct Slot { uint64_t handle = 0; std::optional<Object> value; };
  using TypeNames = std::deque<std::string>;
  uint64_t serial = 0;
  std::shared_ptr<TypeNames> type_names;
  std::array<Slot, 64> local;
  std::unordered_map<uint64_t, Object> objects;
  std::string_view intern(const char *type) {
    if (!type_names) type_names = std::make_shared<TypeNames>();
    const std::string_view wanted(type);
    for (const auto &name : *type_names) if (std::string_view(name) == wanted) return name;
    type_names->emplace_back(type);
    return type_names->back();
  }
  template<class T> uint64_t retain_shared(const char *type, std::shared_ptr<T> value) {
    if (serial == UINT64_MAX) throw Fault{"opaque handle space exhausted"};
    const uint64_t handle = ++serial;
    Object object{intern(type), typeid(T), std::move(value)};
    auto &slot = local[handle % local.size()];
    if (!slot.value) {
      slot.handle = handle;
      slot.value.emplace(std::move(object));
    } else objects.emplace(handle, std::move(object));
    return handle;
  }
  template<class T> uint64_t retain(const char *type, T value) {
    return retain_shared(type, std::make_shared<T>(std::move(value)));
  }
  const Object *find(uint64_t handle) const {
    const auto &slot = local[handle % local.size()];
    if (slot.handle == handle && slot.value) return &*slot.value;
    const auto it = objects.find(handle);
    return it == objects.end() ? nullptr : &it->second;
  }
  Object *find(uint64_t handle) { return const_cast<Object *>(std::as_const(*this).find(handle)); }
  const Object &check(uint64_t handle, const char *type) const {
    const auto *entry = find(handle);
    if (!entry || entry->type != type) throw Fault{"invalid opaque handle"};
    return *entry;
  }
  template<class T> T &object(uint64_t handle, const char *type) const {
    const auto &entry = check(handle, type);
    if (entry.value_type != typeid(T)) throw Fault{"opaque object value type mismatch"};
    return *static_cast<T *>(entry.value.get());
  }
  void release(uint64_t handle) {
    auto &slot = local[handle % local.size()];
    if (slot.handle == handle && slot.value) slot.value.reset();
    else if (!objects.erase(handle)) throw Fault{"invalid opaque handle"};
  }
};
struct Runtime {
  Arena arena;
  virtual Reply call(const char *name, std::span<const u128> arguments) = 0;
  virtual Reply poll(uint64_t ticket) = 0;
  void release_object(uint64_t handle, const char *type) {
    arena.check(handle, type);
    arena.release(handle);
  }
  void snapshot_release(uint64_t handle) { release_object(handle, "warp_snapshot"); }
  void snapshot_release64(uint64_t handle) { release_object(handle, "warp_snapshot64"); }
  void snapshot_release_words(uint64_t handle) { release_object(handle, "warp_word_group"); }
  void barrier_release_ticket(uint64_t handle) { release_object(handle, "barrier_ticket"); }
  void block_barrier_release_ticket(uint64_t handle) { release_object(handle, "block_barrier_ticket"); }
  void warp_sync_release_ticket(uint64_t handle) { release_object(handle, "warp_sync_ticket"); }
  void convergence_release_ticket(uint64_t handle) { release_object(handle, "convergence_ticket"); }
  Reply checked_call(const char *name, std::span<const u128> arguments, size_t results) {
    auto reply = call(name, arguments);
    if (reply.status == Suspended) throw Fault{"non-suspending operation suspended"};
    if (reply.status == Faulted) throw Fault{"ordered runtime fault"};
    if (reply.values.size() != results) throw Fault{"runtime result arity mismatch"};
    return reply;
  }
  uint32_t scalar_call(const char *name, std::span<const u128> arguments) {
    const auto reply = checked_call(name, arguments, 1);
    if (reply.values[0] > UINT32_MAX) throw Fault{"runtime result outside its declared width"};
    return uint32_t(reply.values[0]);
  }
  template<uint32_t Operation> uint32_t snapshot_reduce(uint64_t handle, uint32_t lane = 32) {
    return calculate_snapshot(Operation, handle, lane);
  }
  virtual uint32_t calculate_snapshot(uint32_t operation, uint64_t handle, uint32_t lane) {
    if (operation >= std::size(prim::REDUCTION_NAMES) + 3) throw Fault{"unknown snapshot reduction"};
    arena.check(handle, operation == 2 ? "warp_snapshot64" : "warp_snapshot");
    static constexpr const char *names[] = {"snapshot_ballot", "snapshot_match", "snapshot_match64"};
    const std::string name = operation < 3 ? names[operation] :
                             "snapshot_redux_" + std::string(prim::REDUCTION_NAMES[operation - 3]);
    const std::array<u128, 2> arguments{handle, lane};
    return scalar_call(name.c_str(), std::span(arguments).first(operation == 1 || operation == 2 ? 2 : 1));
  }
  virtual uint32_t snapshot_word(uint64_t handle, uint32_t lane, uint32_t word) {
    arena.check(handle, "warp_word_group");
    return scalar_call("snapshot_word", std::array<u128, 3>{handle, lane, word});
  }
  virtual uint32_t block_barrier_result() { return scalar_call("block_barrier_result", {}); }
  virtual uint32_t barrier_eligible_lanes(uint32_t mask) {
    return scalar_call("barrier_eligible_lanes", std::array<u128, 1>{mask});
  }
  virtual uint32_t block_barrier_state(uint32_t id) {
    return scalar_call("block_barrier_state", std::array<u128, 1>{id});
  }
  virtual Reply block_barrier_wait_deferred() { return call("block_barrier_wait_deferred", {}); }
  virtual Reply warp_capture(uint32_t lane, uint32_t mask, uint32_t value) {
    return call("warp_capture", std::array<u128, 3>{lane, mask, value});
  }
  virtual Reply warp_capture64(uint32_t lane, uint32_t mask, uint64_t value) {
    return call("warp_capture64", std::array<u128, 3>{lane, mask, value});
  }
  virtual Reply warp_capture_predicate(uint32_t lane, uint32_t mask, bool value) {
    return call("warp_capture_predicate", std::array<u128, 3>{lane, mask, value});
  }
  virtual void convergence_enter(uint32_t id, uint64_t target, uint32_t members) {
    checked_call("convergence_enter", std::array<u128, 3>{id, target, members}, 0);
  }
  virtual Reply convergence_wait(uint64_t ticket) {
    return call("convergence_wait", std::array<u128, 1>{ticket});
  }
  virtual bool cooperative_context() {
    const auto value = scalar_call("cooperative_context", {});
    if (value > 1) throw Fault{"runtime result outside its declared width"};
    return bool(value);
  }
  virtual void acquire() = 0;
  virtual void release() = 0;
  virtual ~Runtime() = default;
};
}
