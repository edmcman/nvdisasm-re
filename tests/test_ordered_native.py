import ctypes
import pytest
import sass.semantics.ordered
from sass.emulate import build, ordered
from tests.harness import ordered_gate


pytestmark = pytest.mark.native
MANY_RESULTS = sass.semantics.ordered.operation("ordered_test_many_results", results=(32, 32, 64),
                                               description="Test three typed results from one runtime operation.")


@pytest.fixture(scope="module")
def capture_reply_library():
    specs = (sass.semantics.ordered.CAPTURE, sass.semantics.ordered.CAPTURE64,
             sass.semantics.ordered.CAPTURE_PREDICATE)
    functions = []
    for index, spec in enumerate(specs):
        body = sass.semantics.ordered.Builder()
        previous = body.read_register("R", 1)
        body.emit(sass.semantics.ordered.WriteRegister(("R", 1, 1), previous + 1))
        value = ((1 << 40) | 7) if index == 1 else 1 if index == 2 else 7
        snapshot, = body.special(spec, *(sass.semantics.bitvec.const(v, w)
                                        for v, w in zip((0, 1, value), spec.arguments)))
        body.special(sass.semantics.ordered.RELEASE_SNAPSHOT64 if index == 1 else
                     sass.semantics.ordered.RELEASE_SNAPSHOT, snapshot)
        body.emit(sass.semantics.ordered.WriteRegister(("R", 2, 1), sass.semantics.bitvec.const(42, 32)))
        functions.append(ordered.function(body.block(), f"capture{index}"))
    library = build.library('#include "sass/ordered.hpp"\n' + "\n".join(functions) + """
struct CaptureReplies : sass::ordered::Runtime {
  unsigned operation, mode, calls = 0, polls = 0;
  CaptureReplies(unsigned operation, unsigned mode) : operation(operation), mode(mode) {}
  sass::ordered::Reply completed() {
    const char *type = operation == 1 ? "warp_snapshot64" : "warp_snapshot";
    if (mode == 1) return {};
    if (mode == 2) return {sass::ordered::Complete, {sass::u128(1) << 80}};
    if (mode == 3) type = "barrier_ticket";
    return {sass::ordered::Complete, {arena.retain(type, uint32_t(7))}};
  }
  sass::ordered::Reply call(const char *name, std::span<const sass::u128> arguments) override {
    static const char *names[] = {"warp_capture", "warp_capture64", "warp_capture_predicate"};
    const sass::u128 value = operation == 1 ? (sass::u128(1) << 40) | 7 : operation == 2 ? 1 : 7;
    if (std::string_view(name) != names[operation] || arguments.size() != 3 ||
        arguments[0] != 0 || arguments[1] != 1 || arguments[2] != value)
      throw sass::Fault{"incorrect capture arguments"};
    ++calls;
    if (mode == 4) return {sass::ordered::Faulted};
    if (mode >= 5) return {sass::ordered::Suspended, {}, 77};
    return completed();
  }
  sass::ordered::Reply poll(uint64_t ticket) override {
    if (ticket != 77) throw sass::Fault{"incorrect capture ticket"};
    ++polls;
    if (mode == 6 && polls == 1) return {sass::ordered::Suspended, {}, ticket};
    return completed();
  }
  void acquire() override {}
  void release() override {}
};
extern "C" void capture_reply(unsigned operation, unsigned mode, uint32_t *out) {
  sass::Thread thread{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment environment{};
  sass::Ctx context{thread, memory, local, warp, environment};
  sass::ordered::Frame frame;
  CaptureReplies runtime(operation, mode);
  sass::ordered::Status status = sass::ordered::Suspended;
  for (unsigned turn = 0; turn < 4 && status == sass::ordered::Suspended; ++turn) {
    switch (operation) {
      case 0: status = capture0(context, runtime, frame); break;
      case 1: status = capture1(context, runtime, frame); break;
      case 2: status = capture2(context, runtime, frame); break;
    }
  }
  out[0] = status; out[1] = thread.r[1]; out[2] = thread.r[2];
  out[3] = runtime.calls; out[4] = runtime.polls;
}
""", "ordered-capture-replies")
    library.capture_reply.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    library.capture_reply.restype = None
    return library


@pytest.mark.parametrize("operation", range(3))
@pytest.mark.parametrize("mode", range(7))
def test_native_capture_replies_preserve_effects_types_and_suspension(capture_reply_library, operation, mode):
    out = (ctypes.c_uint32 * 5)()
    capture_reply_library.capture_reply(operation, mode, out)
    failed = mode in range(1, 5)
    assert list(out) == [2 if failed else 0, 1, 0 if failed else 42, 1, max(0, mode - 4)]


@pytest.fixture(scope="module")
def commit_library():
    functions = []
    for board in range(8):
        body = sass.semantics.ordered.Builder()
        body.special(sass.semantics.ordered.ASYNC_COMMIT, sass.semantics.bitvec.const(board, 32))
        functions.append(ordered.function(body.block(), f"commit{board}", runtime=False))
    cases = "".join(f"case {board}: commit{board}(c); break;" for board in range(8))
    library = build.library('#include "sass/ordered.hpp"\n' + "\n".join(functions) + """
extern "C" int commit_slot(unsigned board) {
  sass::Thread t{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment environment{};
  sass::AsyncState state;
  warp.async = &state;
  warp.write_dependency = 7;
  sass::Ctx c{t, memory, local, warp, environment};
  try {
    switch (board) { """ + cases + """ }
    return int(state.pending);
  } catch (const sass::Fault &) { return -1; }
}
""", "ordered-commit-slot")
    library.commit_slot.argtypes = [ctypes.c_uint32]
    library.commit_slot.restype = ctypes.c_int
    return library


@pytest.mark.parametrize("board", range(8))
def test_native_async_commit_uses_its_argument_and_rejects_invalid_slots(commit_library, board):
    assert commit_library.commit_slot(board) == (1 << board if board < 6 else -1)


@pytest.fixture(scope="module")
def wait_library():
    body = sass.semantics.ordered.Builder()
    board, count, slots = (body.read_register("R", index) for index in range(3))
    body.emit(sass.semantics.ordered.WriteRegister(("R", 3, 1), sass.semantics.bitvec.const(99, 32)))
    body.special(sass.semantics.ordered.ASYNC_WAIT_GROUPS, board, count)
    body.emit(sass.semantics.ordered.WriteRegister(("R", 4, 1), sass.semantics.bitvec.const(100, 32)))
    body.special(sass.semantics.ordered.ASYNC_WAIT_SLOTS, sass.semantics.bitvec.trunc(slots, 6))
    function = ordered.function(body.block(), "wait_groups", runtime=False)
    library = build.library('#include "sass/ordered.hpp"\n' + function + """
extern "C" int wait_slots(unsigned board, unsigned count, unsigned mask, uint32_t *out) {
  sass::Thread thread{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment environment{};
  sass::AsyncState state;
  for (unsigned group = 0; group < 2; ++group) {
    state.open.push_back({group * 4, 11 + group, 0, 4});
    state.commit(0);
  }
  state.open.push_back({8, 13, 0, 4});
  state.commit(1);
  warp.async = &state;
  sass::Ctx context{thread, memory, local, warp, environment};
  thread.r[0] = board; thread.r[1] = count; thread.r[2] = mask;
  int status = 0;
  try { wait_groups(context); } catch (const sass::Fault &) { status = -1; }
  out[0] = thread.r[3]; out[1] = thread.r[4]; out[2] = state.pending;
  for (unsigned index = 0; index < 3; ++index) out[index + 3] = memory.load<32>(sass::Shared, index * 4);
  return status;
}
""", "ordered-async-waits")
    library.wait_slots.argtypes = [ctypes.c_uint32] * 3 + [ctypes.POINTER(ctypes.c_uint32)]
    library.wait_slots.restype = ctypes.c_int
    return library


@pytest.mark.parametrize("board", range(8))
@pytest.mark.parametrize("count", (0, 1, 2, 63))
@pytest.mark.parametrize("mask", (0, 1, 2, 3))
def test_native_async_waits_complete_groups_in_order_and_preserve_prior_writes(wait_library, board, count, mask):
    out = (ctypes.c_uint32 * 6)()
    status = wait_library.wait_slots(board, count, mask, out)
    if board >= 6:
        assert status == -1 and list(out) == [99, 0, 3, 0, 0, 0]
        return
    completed = [max(0, 2 - count) if board == 0 else 0, int(board == 1 and count == 0)]
    if mask & 1: completed[0] = 2
    if mask & 2: completed[1] = 1
    pending = int(completed[0] < 2) | int(not completed[1]) << 1
    assert status == 0 and list(out) == [99, 100, pending, 11 if completed[0] else 0,
                                        12 if completed[0] == 2 else 0, 13 if completed[1] else 0]


@pytest.mark.parametrize("protocol", ["warp", "convergence"])
def test_native_issued_wait_retains_its_generation_after_ticket_release(protocol):
    match protocol:
        case "warp":
            prefix, argument, setup, arrive, ready = "warp_sync", "3", "", "arrive_warp", "->ready"
        case "convergence":
            prefix, argument, arrive, ready = "convergence", "0", "arrive_convergence", ".generation->ready"
            setup = "for (unsigned index = 0; index < 2; ++index) coordination.enter_convergence(index, 0, 0x2000, 3);"
    source = """
#include "sass/ordered_runtime.hpp"
extern "C" unsigned retained_warp_wait() {
  sass::Coordination coordination;
  coordination.reset(2);
  sass::Thread thread{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment environment{};
  warp.coordination = &coordination;
  sass::Ctx context{thread, memory, local, warp, environment};
  sass::ordered::SchedulerRuntime runtime;
  runtime.bind(context, 0, 2);
  $setup
  auto ticket = runtime.call("$prefix_arrive", std::array<sass::u128, 1>{$argument}).values[0];
  auto pending = runtime.call("$prefix_wait", std::array<sass::u128, 1>{ticket});
  if (pending.status != sass::ordered::Suspended) return 1;
  runtime.$prefix_release_ticket(uint64_t(ticket));
  if (runtime.poll(pending.ticket).status != sass::ordered::Suspended) return 2;
  auto peer = coordination.$arrive(1, $argument);
  if (runtime.poll(pending.ticket).status != sass::ordered::Complete) return 3;
  if (runtime.arena.find(pending.ticket) || runtime.arena.find(uint64_t(ticket))) return 4;
  $setup
  auto reused = coordination.$arrive(0, $argument);
  if (reused$ready || !peer$ready) return 5;
  coordination.$arrive(1, $argument);
  if (!reused$ready || !peer$ready) return 6;
  try { runtime.poll(pending.ticket); return 7; } catch (const sass::Fault &) {}
  return 0;
}
"""
    for name, value in dict(prefix=prefix, argument=argument, setup=setup, arrive=arrive, ready=ready).items():
        source = source.replace("$" + name, value)
    library = build.library(source, "ordered-retained-wait")
    library.retained_warp_wait.restype = ctypes.c_uint
    assert library.retained_warp_wait() == 0


def test_native_retained_snapshots_survive_later_generations_and_release():
    library = build.library("""
#include "sass/ordered_runtime.hpp"
extern "C" unsigned retained_snapshots() {
  sass::Thread thread{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment environment{};
  sass::Ctx context{thread, memory, local, warp, environment};
  sass::ordered::SchedulerRuntime runtime;
  sass::ordered::Frame frame;
  runtime.bind(context, frame, 0, 1);
  std::array<uint64_t, 257> handles{};
  for (unsigned generation = 0; generation < handles.size(); ++generation) {
    handles[generation] = uint64_t(runtime.call("warp_capture",
      std::array<sass::u128, 3>{0, 1, generation * 7}).values[0]);
    if (runtime.calculate_snapshot(0, handles[generation], 32) != bool(generation)) return 3;
    if (runtime.calculate_snapshot(3 + sass::prim::Add, handles[generation], 32) != generation * 7) return 4;
  }
  for (unsigned generation = 0; generation < handles.size(); ++generation) {
    if (runtime.call("snapshot_lane", std::array<sass::u128, 2>{handles[generation], 0}).values[0] != generation * 7)
      return 1;
    if (runtime.calculate_snapshot(0, handles[generation], 32) != bool(generation)) return 5;
    if (runtime.calculate_snapshot(3 + sass::prim::Add, handles[generation], 32) != generation * 7) return 6;
  }
  for (auto handle : handles) runtime.call("snapshot_release", std::array<sass::u128, 1>{handle});
  for (unsigned generation = 0; generation < handles.size(); ++generation) {
    auto handle = runtime.call("warp_capture", std::array<sass::u128, 3>{0, 1, generation}).values[0];
    runtime.call("snapshot_release", std::array<sass::u128, 1>{handle});
  }
  for (auto handle : handles) {
    try { runtime.call("snapshot_lane", std::array<sass::u128, 2>{handle, 0}); return 2; }
    catch (const sass::Fault &) {}
  }
  return 0;
}
""", "ordered-retained-snapshots")
    library.retained_snapshots.restype = ctypes.c_uint
    assert library.retained_snapshots() == 0


@pytest.fixture(scope="module")
def snapshot_binding_library():
    specs = [sass.semantics.ordered.BALLOT, sass.semantics.ordered.MATCH, sass.semantics.ordered.MATCH64,
             *sass.semantics.ordered.REDUCTIONS.values()]
    cases = "".join(f"case {index}: return runtime.{spec.native}(handle"
                    f"{', 0' if len(spec.arguments) == 2 else ''});" for index, spec in enumerate(specs))
    names = ", ".join(f'"{spec.name}"' for spec in specs)
    releases = [sass.semantics.ordered.RELEASE_SNAPSHOT, sass.semantics.ordered.RELEASE_SNAPSHOT64,
                sass.semantics.ordered.RELEASE_WORDS, sass.semantics.ordered.RELEASE_TICKET]
    release_cases = "".join(f"case {index}: runtime.{spec.native}(handle); break;"
                            for index, spec in enumerate(releases))
    types = ", ".join(f'"{spec.arguments[0].name}"' for spec in releases)
    library = build.library("""
#include "sass/ordered.hpp"
struct Replies : sass::ordered::Runtime {
  unsigned mode, operation;
  explicit Replies(unsigned mode, unsigned operation = 0) : mode(mode), operation(operation) {}
  sass::ordered::Reply call(const char *name, std::span<const sass::u128> arguments) override {
    static constexpr const char *names[] = { """ + names + """ };
    const bool matching = operation == 1 || operation == 2;
    if (std::string(name) != names[operation] || arguments.size() != (matching ? 2 : 1) ||
        (matching && arguments[1] != 0)) throw sass::Fault{"incorrect snapshot binding"};
    switch (mode) {
      case 1: return {sass::ordered::Suspended};
      case 2: return {sass::ordered::Faulted};
      case 3: return {};
      case 4: return {sass::ordered::Complete, {1, 2}};
      case 5: return {sass::ordered::Complete, {sass::u128(1) << 32}};
      default: return {sass::ordered::Complete, {342}};
    }
  }
  sass::ordered::Reply poll(uint64_t) override { return {}; }
  void acquire() override {}
  void release() override {}
};
extern "C" int snapshot_binding(unsigned operation, unsigned mode) {
  Replies runtime(mode, operation);
  const bool wide = (operation == 2) != (mode == 6);
  auto handle = runtime.arena.retain(wide ? "warp_snapshot64" : "warp_snapshot", uint32_t(0));
  if (mode == 7) runtime.arena.release(handle);
  try { switch (operation) { """ + cases + """ } }
  catch (const sass::Fault &) { return -1; }
  return -2;
}
extern "C" int snapshot_release_binding(unsigned operation, unsigned mode) {
  Replies runtime(0);
  static constexpr const char *types[] = { """ + types + """ };
  const char *type = mode == 1 ? "incorrect_opaque_type" : types[operation];
  auto handle = runtime.arena.retain(type, uint32_t(0));
  if (mode == 2) runtime.arena.release(handle);
  const auto release = [&] { switch (operation) { """ + release_cases + """ } };
  try {
    release();
    try { release(); return -2; }
    catch (const sass::Fault &) { return 0; }
  } catch (const sass::Fault &) { return -1; }
}
extern "C" int retained_type_name() {
  sass::ordered::Arena retained;
  uint64_t handle;
  {
    sass::ordered::Arena source;
    std::string type = "caller_owned_type_name";
    handle = source.retain(type.c_str(), uint32_t(42));
    retained = source;
    type.assign("replaced_type_name");
    if (source.object<uint32_t>(handle, "caller_owned_type_name") != 42) return 1;
  }
  if (retained.object<uint32_t>(handle, "caller_owned_type_name") != 42) return 2;
  retained.release(handle);
  try { retained.object<uint32_t>(handle, "caller_owned_type_name"); return 3; }
  catch (const sass::Fault &) { return 0; }
}
""", "ordered-snapshot-bindings")
    library.snapshot_binding.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    library.snapshot_binding.restype = ctypes.c_int
    library.snapshot_release_binding.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    library.snapshot_release_binding.restype = ctypes.c_int
    library.retained_type_name.restype = ctypes.c_int
    return library


@pytest.mark.parametrize("operation", range(11))
@pytest.mark.parametrize("mode", range(8))
def test_native_snapshot_bindings_validate_runtime_replies_and_handles(snapshot_binding_library, operation, mode):
    assert snapshot_binding_library.snapshot_binding(operation, mode) == (342 if mode == 0 else -1)


@pytest.mark.parametrize("operation", range(4))
@pytest.mark.parametrize("mode", range(3))
def test_native_release_bindings_preserve_type_and_lifetime_checks(snapshot_binding_library, operation, mode):
    assert snapshot_binding_library.snapshot_release_binding(operation, mode) == (0 if mode == 0 else -1)


def test_native_type_names_outlive_callers_and_copied_arenas(snapshot_binding_library):
    assert snapshot_binding_library.retained_type_name() == 0


def test_native_ticket_release_keeps_arrivals_and_pending_waits():
    library = build.library("""
#include "sass/ordered_runtime.hpp"
extern "C" unsigned ticket_release() {
  sass::Thread thread{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment environment{};
  sass::Ctx context{thread, memory, local, warp, environment};
  sass::ordered::SchedulerRuntime runtime;
  runtime.bind(context, 0, 2);
  const auto first = runtime.call("barrier_arrive", std::array<sass::u128, 3>{0, 2, 1}).values[0];
  auto pending = runtime.call("barrier_wait", std::array<sass::u128, 1>{first});
  if (pending.status != sass::ordered::Suspended) return 1;
  runtime.call("barrier_release_ticket", std::array<sass::u128, 1>{first});
  if (runtime.poll(pending.ticket).status != sass::ordered::Suspended) return 2;
  for (const char *name : {"barrier_wait", "barrier_release_ticket"}) {
    try { runtime.call(name, std::array<sass::u128, 1>{first}); return 3; } catch (const sass::Fault &) {}
  }
  const auto other = runtime.arena.retain("warp_snapshot", uint32_t(7));
  try { runtime.call("barrier_release_ticket", std::array<sass::u128, 1>{other}); return 4; }
  catch (const sass::Fault &) {}
  runtime.arena.check(other, "warp_snapshot");
  runtime.arena.release(other);
  runtime.bind(context, 1, 2);
  const auto last = runtime.call("barrier_arrive", std::array<sass::u128, 3>{0, 2, 0}).values[0];
  runtime.call("barrier_release_ticket", std::array<sass::u128, 1>{last});
  const auto result = runtime.poll(pending.ticket);
  if (result.status != sass::ordered::Complete || result.values.size() != 1 || result.values[0] != 1 ||
      !runtime.barriers.empty()) return 5;
  for (const char *name : {"snapshot_ballot", "snapshot_match", "snapshot_match64", "snapshot_redux_add"}) {
    try { runtime.call(name, std::span<const sass::u128>{}); return 6; } catch (const sass::Fault &) {}
  }
  return 0;
}
""", "ordered-ticket-release")
    library.ticket_release.restype = ctypes.c_uint
    assert library.ticket_release() == 0


@pytest.fixture(scope="module")
def word_snapshot_library():
    library = build.library("""
#include "sass/ordered_runtime.hpp"
extern "C" unsigned word_snapshots(unsigned count) {
  sass::Thread thread{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment environment{};
  sass::Ctx context{thread, memory, local, warp, environment};
  sass::ordered::SchedulerRuntime runtime;
  sass::ordered::Frame frame;
  const char *operation = count == 1 ? "warp_capture_words1" : count == 2 ? "warp_capture_words2" : "warp_capture_words4";
  std::array<uint64_t, 2> retained{};
  for (unsigned generation = 0; generation < 2; ++generation) {
    uint64_t ticket = 0;
    for (unsigned lane = 0; lane < 2; ++lane) {
      runtime.bind(context, frame, lane, 2);
      sass::u128 value = 0;
      for (unsigned word = 0; word < count; ++word)
        value |= sass::u128(generation * 1000 + lane * 100 + word) << (word * 32);
      auto result = runtime.call(operation, std::array<sass::u128, 4>{0, lane, 3, value});
      if (!lane) {
        if (result.status != sass::ordered::Suspended) return 1;
        ticket = result.ticket;
      } else {
        if (result.status != sass::ordered::Complete) return 2;
        runtime.call("snapshot_release_words", std::array<sass::u128, 1>{result.values[0]});
      }
    }
    auto result = runtime.poll(ticket);
    if (result.status != sass::ordered::Complete) return 3;
    retained[generation] = uint64_t(result.values[0]);
  }
  for (unsigned generation = 0; generation < 2; ++generation) {
    const uint64_t handle = retained[generation];
    for (unsigned lane = 0; lane < 2; ++lane)
      for (unsigned word = 0; word < count; ++word)
        if (runtime.call("snapshot_word", std::array<sass::u128, 3>{handle, lane, word}).values[0] !=
            generation * 1000 + lane * 100 + word) return 4;
    for (auto arguments : {std::array<sass::u128, 3>{handle, 2, 0}, {handle, 0, count}}) {
      try { runtime.call("snapshot_word", arguments); return 5; } catch (const sass::Fault &) {}
    }
    runtime.call("snapshot_release_words", std::array<sass::u128, 1>{handle});
    try { runtime.call("snapshot_word", std::array<sass::u128, 3>{handle, 0, 0}); return 6; }
    catch (const sass::Fault &) {}
  }
  return 0;
}
""", "ordered-word-snapshots")
    library.word_snapshots.argtypes = [ctypes.c_uint32]
    library.word_snapshots.restype = ctypes.c_uint32
    return library


@pytest.mark.parametrize("count", (1, 2, 4))
def test_native_word_snapshots_retain_all_words_and_validate_reads(word_snapshot_library, count):
    assert word_snapshot_library.word_snapshots(count) == 0


def source():
    bodies = [ordered_gate.register_body(), ordered_gate.branch_body(True), ordered_gate.atomic_body(),
              ordered_gate.suspension_body(), ordered_gate.snapshot_body(), ordered_gate.atomic_body(True)]
    functions = [ordered.function(body, f"gate{i}", 16) for i, body in enumerate(bodies)]
    many = sass.semantics.ordered.Builder()
    a, b, c = many.special(MANY_RESULTS)
    many.assign_from_inputs([(("R", 0, 1), a), (("R", 1, 1), b), (("R", 2, 2), c)])
    functions.append(ordered.function(many.block(), "gate_many", 16))
    environment = sass.semantics.ordered.Builder()
    lane, = environment.special(sass.semantics.ordered.LANE)
    active, = environment.special(sass.semantics.ordered.ACTIVE)
    environment.assign_from_inputs([(("R", 0, 1), lane), (("R", 1, 1), active)])
    functions.append(ordered.function(environment.block(), "gate_environment", 16, runtime=False))
    return '#include "sass/ordered.hpp"\n' + "\n".join(functions) + """
struct Runtime : sass::ordered::Runtime {
  bool ready = false, owned = false, invalid = false;
  unsigned arrivals = 0, waits = 0;
  Runtime *shared = nullptr;
  unsigned mask = 0;
  uint64_t snapshot = 0;
  uint32_t values[32]{};
  sass::ordered::Reply call(const char *name, std::span<const sass::u128> args) override {
    if (shared) return shared->call(name, args);
    std::string n(name);
    if (n == "ordered_test_many_results") return {sass::ordered::Complete, {42, 43, (sass::u128(1) << 32) | 44}};
    if (n == "barrier_arrive") {
      ++arrivals;
      return {sass::ordered::Complete, {arena.retain(invalid ? "warp_snapshot" : "barrier_ticket", uint32_t(7))}};
    }
    if (n == "barrier_wait" || n == "barrier_wait_ready") {
      ++waits; return {sass::ordered::Suspended, {}, n == "barrier_wait" ? 7u : 9u};
    }
    if (n == "warp_capture") {
      const unsigned lane = unsigned(args[0]);
      if (lane >= 32) throw sass::Fault{"capture lane out of range"};
      if (mask >> lane & 1) throw sass::Fault{"capture repeats"};
      values[lane] = uint32_t(args[2]); mask |= 1u << lane;
      if (mask == unsigned(args[1])) snapshot = arena.retain("warp_snapshot", std::vector<uint32_t>(values, values + 32));
      return mask == unsigned(args[1]) ? sass::ordered::Reply{sass::ordered::Complete, {snapshot}} :
                                        sass::ordered::Reply{sass::ordered::Suspended, {}, 8};
    }
    if (n == "snapshot_lane") return {sass::ordered::Complete, {arena.object<std::vector<uint32_t>>(uint64_t(args[0]), "warp_snapshot")[unsigned(args[1])]}};
    throw sass::Fault{"gate failure"};
  }
  sass::ordered::Reply poll(uint64_t ticket) override {
    if (shared) return shared->poll(ticket);
    if (ticket == 8) return mask == 3 ? sass::ordered::Reply{sass::ordered::Complete, {snapshot}} :
                                       sass::ordered::Reply{sass::ordered::Suspended, {}, 8};
    return ready ? sass::ordered::Reply{sass::ordered::Complete, ticket == 7 ? sass::ordered::Values{3} : sass::ordered::Values{}} :
                   sass::ordered::Reply{sass::ordered::Suspended, {}, ticket};
  }
  void acquire() override { if (owned) throw sass::Fault{"owned"}; owned = true; }
  void release() override { owned = false; }
};
struct Gate {
  sass::Thread t{};
  sass::Memory memory, local;
  sass::WarpContext warp{};
  sass::Environment environment{};
  sass::ordered::Frame frame;
  Runtime runtime;
  Gate(unsigned mode) {
    t.r[0] = mode == 4 ? 0 : 7; t.r[1] = 10; t.p[0] = 1;
    runtime.invalid = mode == 6;
    if (mode == 7) { warp.present = 1; warp.lane = 2; warp.active = 7; }
    memory.store<32>(sass::Ram, 16, 23);
  }
};
extern "C" void *gate_new(unsigned mode) { return new Gate(mode); }
extern "C" void gate_free(Gate *g) { delete g; }
extern "C" uint32_t gate_register(Gate *g, unsigned index) { return g->t.r[index]; }
extern "C" void gate_link(Gate *a, Gate *b) { b->runtime.shared = &a->runtime; b->t.r[0] = 1; b->t.r[1] = 11; }
extern "C" unsigned gate_run(Gate *g, unsigned mode, unsigned ready, uint64_t *out) {
  auto &runtime = g->runtime.shared ? *g->runtime.shared : g->runtime;
  runtime.ready = ready;
  sass::Ctx c{g->t, g->memory, g->local, g->warp, g->environment};
  unsigned status;
  try {
    switch (mode) {
      case 0: status = gate0(c, runtime); break;
      case 1: status = gate1(c, runtime); break;
      case 2: status = gate2(c, runtime); break;
      case 3: status = gate3(c, runtime, g->frame); break;
      case 4: status = gate4(c, runtime, g->frame); break;
      case 6: status = gate3(c, runtime, g->frame); break;
      case 7: gate_environment(c); status = sass::ordered::Complete; break;
      case 8: status = gate_many(c, runtime); break;
      default: status = gate5(c, runtime); break;
    }
  } catch (const sass::Fault &) { status = sass::ordered::Faulted; }
  out[0] = g->t.r[0]; out[1] = g->t.r[1]; out[2] = g->t.r[2]; out[3] = g->t.pc;
  out[4] = g->memory.load<32>(sass::Ram, 16);
  out[5] = runtime.arrivals; out[6] = runtime.waits; out[7] = runtime.owned;
  return status;
}
"""


@pytest.fixture(scope="module")
def library():
    lib = build.library(source(), "ordered-gate")
    lib.gate_new.argtypes, lib.gate_new.restype = [ctypes.c_uint], ctypes.c_void_p
    lib.gate_free.argtypes = [ctypes.c_void_p]
    lib.gate_register.argtypes, lib.gate_register.restype = [ctypes.c_void_p, ctypes.c_uint], ctypes.c_uint32
    lib.gate_link.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.gate_run.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint64)]
    lib.gate_run.restype = ctypes.c_uint
    return lib


def test_native_runtime_delivers_more_than_two_typed_results(library):
    gate = library.gate_new(8)
    out = (ctypes.c_uint64 * 8)()
    try:
        assert library.gate_run(gate, 8, 0, out) == 0
        assert tuple(out[:4]) == (42, 43, 44, 16)
        assert library.gate_register(gate, 3) == 1
    finally: library.gate_free(gate)


@pytest.mark.parametrize("mode,expected", [(0, (8, 8, 0, 16, 23)), (1, (23, 10, 0, 16, 23)),
                                           (2, (7, 10, 0, 16, 24)), (5, (7, 10, 0, 0, 24)),
                                           (7, (2, 7, 0, 16, 23))])
def test_ordered_native(library, mode, expected):
    gate = library.gate_new(mode)
    out = (ctypes.c_uint64 * 8)()
    try:
        assert library.gate_run(gate, mode, 0, out) == (2 if mode == 5 else 0)
        assert tuple(out[:5]) == expected
        assert out[7] == 0
    finally: library.gate_free(gate)


@pytest.mark.parametrize("mode", [3])
def test_native_resume(library, mode):
    gate = library.gate_new(mode)
    out = (ctypes.c_uint64 * 8)()
    try:
        assert library.gate_run(gate, mode, 0, out) == 1
        assert out[3] == 0 and tuple(out[5:7]) == (1, 1)
        before = tuple(out)
        assert library.gate_run(gate, mode, 0, out) == 1
        assert tuple(out) == before
        assert library.gate_run(gate, mode, 1, out) == 0
        assert out[3] == 16 and tuple(out[5:7]) == (1, 1)
        assert out[1] == 10
        assert library.gate_run(gate, mode, 1, out) == 0
        assert tuple(out[5:7]) == (1, 1)
    finally: library.gate_free(gate)


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_native_collective_capture_before_any_destination_write(library, order):
    gates = [library.gate_new(4) for _ in range(2)]
    library.gate_link(*gates)
    outputs = [(ctypes.c_uint64 * 8)() for _ in gates]
    try:
        lane = order[0]
        assert library.gate_run(gates[lane], 4, 0, outputs[lane]) == 1
        assert outputs[lane][1] == 10 + lane
        lane = order[1]
        assert library.gate_run(gates[lane], 4, 0, outputs[lane]) == 1
        assert outputs[lane][1] == 99
        status = [1, 1]
        for _ in range(3):
            for lane in order:
                status[lane] = library.gate_run(gates[lane], 4, 1, outputs[lane])
                assert status[lane] in (0, 1)
        assert status == [0, 0]
        assert [out[2] for out in outputs] == [11, 10]
        assert tuple(outputs[order[-1]][5:7]) == (2, 2)
    finally:
        for gate in reversed(gates): library.gate_free(gate)


def test_only_values_used_after_a_pause_are_retained():
    code = sass.semantics.ordered.linearize(ordered_gate.suspension_body())
    assert sass.semantics.ordered.retained_temporaries(code) == {0}
    function = ordered.function(ordered_gate.suspension_body(), "resume")
    assert "f.values.resize(1)" in function


def test_native_rejects_a_handle_of_the_wrong_opaque_type(library):
    gate = library.gate_new(6)
    out = (ctypes.c_uint64 * 8)()
    try:
        assert library.gate_run(gate, 6, 0, out) == 2
        assert out[0] == 8 and out[3] == 0
        assert tuple(out[5:7]) == (1, 0)
    finally: library.gate_free(gate)
