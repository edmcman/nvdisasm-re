// C entry points every generated library exports for Python (sass.emulate.native): memory images.
#pragma once
#include <cstdio>
#include <algorithm>
#include "sass/machine.hpp"

extern "C" {
sass::Coordination *sass_coordination_new(uint32_t count) {
  auto *state = new sass::Coordination;
  state->reset(count);
  return state;
}
void sass_coordination_free(sass::Coordination *state) { delete state; }
int sass_convergence_enter(sass::Coordination *state, uint32_t participant, uint32_t id,
    uint64_t target, uint32_t members, char *message, size_t size) {
  try { state->enter_convergence(participant, id, target, members); return 0; }
  catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return 1;
  }
}
sass::Coordination::ConvergenceTicket *sass_convergence_arrive(sass::Coordination *state,
    uint32_t participant, uint32_t id, char *message, size_t size) {
  try { return new sass::Coordination::ConvergenceTicket(state->arrive_convergence(participant, id)); }
  catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return nullptr;
  }
}
void sass_convergence_ticket_free(sass::Coordination::ConvergenceTicket *ticket) { delete ticket; }
uint32_t sass_convergence_ready(const sass::Coordination::ConvergenceTicket *ticket) {
  return ticket->generation->ready;
}
int sass_convergence_check(sass::Coordination *state, const sass::Coordination::ConvergenceTicket *ticket,
    uint32_t participant, char *message, size_t size) {
  try { state->check_convergence(*ticket, participant); return 0; }
  catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return 1;
  }
}
sass::Coordination::WarpTicket *sass_warp_sync_arrive(sass::Coordination *state, uint32_t participant,
    uint32_t mask, char *message, size_t size) {
  try { return new sass::Coordination::WarpTicket(state->arrive_warp(participant, mask)); }
  catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return nullptr;
  }
}
void sass_warp_sync_ticket_free(sass::Coordination::WarpTicket *ticket) { delete ticket; }
uint32_t sass_warp_sync_ready(const sass::Coordination::WarpTicket *ticket) { return (*ticket)->ready; }
int sass_warp_sync_check(sass::Coordination *state, const sass::Coordination::WarpTicket *ticket,
    uint32_t participant, char *message, size_t size) {
  try { state->check_warp(*ticket, participant); return 0; }
  catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return 1;
  }
}
sass::Coordination::Ticket *sass_block_barrier_arrive(sass::Coordination *state, uint32_t participant,
    uint32_t id, uint32_t count, uint32_t predicate, uint32_t reduction, uint32_t implicit,
    char *message, size_t size) {
  try {
    auto ticket = implicit ? state->arrive_implicit(participant, id, predicate, reduction) :
                             state->arrive_counted(participant, id, count, predicate, reduction);
    return new sass::Coordination::Ticket(std::move(ticket));
  } catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return nullptr;
  }
}
void sass_block_barrier_ticket_free(sass::Coordination::Ticket *ticket) { delete ticket; }
uint32_t sass_block_barrier_ready(const sass::Coordination::Ticket *ticket) { return ticket->generation->ready; }
int sass_block_barrier_bind(sass::Coordination *state, const sass::Coordination::Ticket *ticket,
    uint32_t participant, uint32_t deferred, char *message, size_t size) {
  try {
    if (deferred) state->defer(*ticket, participant);
    else state->wait(*ticket, participant);
    return 0;
  } catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return 1;
  }
}
sass::Coordination::Ticket *sass_block_barrier_deferred(sass::Coordination *state, uint32_t participant,
    char *message, size_t size) {
  try {
    auto generation = state->deferred(participant);
    return generation ? new sass::Coordination::Ticket{generation, participant} : nullptr;
  } catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return nullptr;
  }
}
int sass_barrier_eligible_lanes(sass::Coordination *state, uint32_t participant, uint32_t mask,
    uint32_t *value, char *message, size_t size) {
  try { *value = state->eligible_lanes(participant, mask); return 0; }
  catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return 1;
  }
}
int sass_coordination_exit(sass::Coordination *state, uint32_t participant, char *message, size_t size) {
  try { state->exit(participant); return 0; }
  catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return 1;
  }
}
int sass_block_barrier_read(sass::Coordination *state, uint32_t participant, uint32_t id,
    uint32_t current, uint32_t *value, char *message, size_t size) {
  try { *value = current ? state->result(participant) : state->state(id); return 0; }
  catch (const sass::Fault &error) {
    std::snprintf(message, size, "%s", error.message.c_str());
    return 1;
  }
}
sass::Memory *sass_memory_new(const sass::Memory *base) { auto *m = new sass::Memory; m->base = base; return m; }
void sass_memory_free(sass::Memory *m) { delete m; }
void sass_memory_write(sass::Memory *m, int space, uint64_t address, const uint8_t *data, size_t n) {
  for (size_t i = 0; i < n; ++i) m->write(space, address + i, data[i]);
}
void sass_memory_read(const sass::Memory *m, int space, uint64_t address, uint8_t *data, size_t n) {
  for (size_t i = 0; i < n; ++i) data[i] = m->read(space, address + i);
}
sass::CacheState *sass_cache_new() { return new sass::CacheState(); }
void sass_cache_free(sass::CacheState *s) { delete s; }
uint64_t sass_cache_generation(const sass::CacheState *s) { return s->generation; }
int sass_cache_issue(sass::CacheState *s, uint32_t operation, uint64_t address, char *message, size_t size) {
  auto status = s->issue(operation, address);
  if (status) return 0;
  std::snprintf(message, size, "%s", status.message().c_str());
  return 1;
}
size_t sass_cache_requests(const sass::CacheState *s, sass::CacheRequest *out, size_t size) {
  if (size >= s->requests.size()) std::copy(s->requests.begin(), s->requests.end(), out);
  return s->requests.size();
}
void sass_async_issue(sass::AsyncState *s, uint64_t address, uint64_t low, uint64_t high, uint32_t size) {
  s->open.push_back({address, low, high, size});
}
void sass_async_commit(sass::AsyncState *s, uint32_t board) { s->commit(board); }
void sass_async_wait_groups(sass::AsyncState *s, uint32_t board, uint32_t count) { s->wait(board, count); }
void sass_async_wait_slots(sass::AsyncState *s, uint32_t mask) { s->waits(mask); }
size_t sass_async_ready(sass::AsyncState *s, sass::AsyncCopy *out, size_t size) {
  if (size < s->ready.size()) return s->ready.size();
  std::copy(s->ready.begin(), s->ready.end(), out);
  size_t count = s->ready.size();
  s->ready.clear();
  return count;
}
}

namespace sass {
inline int report(const Fault &f, char *message, size_t size) { std::snprintf(message, size, "%s", f.message.c_str()); return 1; }
}
