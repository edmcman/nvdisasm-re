import pytest
from sass.assemble import assemble_instruction
from sass.emulate import callbacks, reference
from sass.semantics import bitvec, lifter, ordered
from tests.harness import ordered_gate


pytestmark = pytest.mark.native


class Kernel(reference.Kernel):
    def __init__(self, request, body):
        super().__init__(request, "SM89")
        self.body = body
        self.lanes = []

    def lift(self, pc):
        return lifter.Semantics("SM89", "ordered", "ordered", pc, "native", body=self.body) if pc == self.base else super().lift(pc)

    def group(self, threads, scope):
        lanes = super().group(threads, scope)
        for index, lane in zip(threads, lanes):
            lane.state.write("R", 0, 1, index % self.block % 32)
            lane.state.write("R", 1, 1, index + 10)
            lane.state.write("R", 3, 1, index % self.block)
        self.lanes.extend(lanes)
        return lanes


def request(schedule="round_robin", scope="launch", threads=2):
    code = b"".join(assemble_instruction("SM89", text) for text in ("NOP", "EXIT"))
    return dict(kernel=dict(code=code.hex(), address=0x1000, threads=threads, block_size=threads,
                            scope=scope, schedule=schedule, trace=True, trace_threads=threads, max_steps=2),
                output=dict(address=0x2000))


@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
def test_shared_scheduler_retains_frames_and_counts_only_completions(schedule):
    spec = request(schedule)
    engine = Kernel(spec, ordered_gate.snapshot_body())
    result = callbacks.run(spec, engine, "SM89")
    assert result["steps"] == 4
    assert [lane.state.read("R", 2, 1) for lane in engine.lanes] == [11, 10]
    trace = result["trace"]
    assert len(trace) == 4
    assert sorted((entry["thread"], entry["pc"]) for entry in trace) == [(0, 0), (0, 16), (1, 0), (1, 16)]
    assert all(entry["writes"] == {"R1": 99, "R2": 11 - entry["thread"]} for entry in trace if entry["pc"] == 0)
    assert all(lane.frame is None for lane in engine.lanes)


def test_shared_scheduler_detects_stalled_frames_without_counting_polls():
    spec = request(scope="thread", threads=1)
    engine = Kernel(spec, ordered_gate.snapshot_body())
    with pytest.raises(RuntimeError, match="no thread can proceed"):
        callbacks.run(spec, engine, "SM89")
    lane, = engine.lanes
    assert lane.state.pc == 0x1000
    assert lane.state.read("R", 1, 1) == 10
    assert lane.frame.status == ordered.Status.SUSPENDED


def test_shared_scheduler_preserves_writes_and_releases_atomic_on_fault():
    spec = request(threads=1)
    engine = Kernel(spec, ordered_gate.atomic_body(True))
    with pytest.raises(RuntimeError, match=f"ordered operation {ordered_gate.FAIL.name}"):
        callbacks.run(spec, engine, "SM89")
    lane, = engine.lanes
    assert lane.state.load("ram", 16, 32) == 1
    assert lane.state.pc == 0x1000
    assert lane.ordered_runtime.owner is None
    assert lane.frame.status == ordered.Status.FAULT


def test_shared_scheduler_keeps_the_selected_path_when_a_guard_changes():
    builder = ordered.Builder()
    guard = builder.read_register("P", 0)
    yes = ordered.Builder(builder.count)
    yes.emit(ordered.WriteRegister(("P", 0, 1), bitvec.const(0, 1)))
    ticket, = yes.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(2, 32), bitvec.const(1, 1))
    yes.special(ordered.WAIT_READY, ticket)
    yes.emit(ordered.WriteRegister(("R", 2, 1), bitvec.const(42, 32)))
    builder.emit(ordered.If(guard, yes.block()))
    spec = request()
    spec["kernel"]["registers"] = {"P0": 1}
    engine = Kernel(spec, builder.block())
    result = callbacks.run(spec, engine, "SM89")
    assert result["steps"] == 4
    assert [lane.state.read("R", 2, 1) for lane in engine.lanes] == [42, 42]


@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
def test_shared_scheduler_runs_peers_at_later_pcs_while_a_frame_waits(schedule):
    first = ordered.Builder()
    lane = first.read_register("R", 0)
    wait = ordered.Builder(first.count)
    ticket, = wait.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(2, 32), bitvec.const(1, 1))
    wait.special(ordered.WAIT_READY, ticket)
    wait.emit(ordered.WriteRegister(("R", 2, 1), bitvec.const(7, 32)))
    first.emit(ordered.If(lane == 0, wait.block()))
    second = ordered.Builder()
    lane = second.read_register("R", 0)
    arrive = ordered.Builder(second.count)
    arrive.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(2, 32), bitvec.const(1, 1))
    arrive.emit(ordered.WriteRegister(("R", 2, 1), bitvec.const(8, 32)))
    second.emit(ordered.If(lane == 1, arrive.block()))

    class DifferentPCs(Kernel):
        def lift(self, pc):
            return lifter.Semantics("SM89", "ordered", "ordered", pc, "native", body=second.block()) \
                if pc == self.base + 16 else super().lift(pc)

    spec = request(schedule)
    spec["kernel"].update(code=b"".join(assemble_instruction("SM89", text) for text in ("NOP", "NOP", "EXIT")).hex(), max_steps=3)
    engine = DifferentPCs(spec, first.block())
    result = callbacks.run(spec, engine, "SM89")
    assert result["steps"] == 6 and len(result["trace"]) == 6
    assert [lane.state.read("R", 2, 1) for lane in engine.lanes] == [7, 8]


@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
def test_shared_scheduler_resumes_a_ready_frame_while_a_peer_loops(schedule):
    first = ordered.Builder()
    lane = first.read_register("R", 0)
    wait = ordered.Builder(first.count)
    ticket, = wait.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(2, 32), bitvec.const(1, 1))
    wait.special(ordered.WAIT_READY, ticket)
    wait.emit(ordered.WriteMemory("ram", bitvec.const(16, 64), bitvec.const(1, 32)))
    first.emit(ordered.If(lane == 0, wait.block()))
    second = ordered.Builder()
    lane = second.read_register("R", 0)
    arrive = ordered.Builder(second.count)
    arrive.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(2, 32), bitvec.const(1, 1))
    second.emit(ordered.If(lane == 1, arrive.block()))
    loop = ordered.Builder()
    ready = loop.read_memory("ram", bitvec.const(16, 64), 32)
    loop.emit(ordered.If(ready == 0, ordered.Block((ordered.Transfer("goto", bitvec.const(0x1020, 64)),))))

    class LoopingPeer(Kernel):
        def lift(self, pc):
            bodies = {self.base: first.block(), self.base + 16: second.block(), self.base + 32: loop.block()}
            return lifter.Semantics("SM89", "ordered", "ordered", pc, "native", body=bodies[pc]) \
                if pc in bodies else super().lift(pc)

    spec = request(schedule)
    spec["kernel"].update(code=b"".join(assemble_instruction("SM89", text) for text in ("NOP", "NOP", "NOP", "EXIT")).hex(),
                          max_steps=4)
    engine = LoopingPeer(spec, first.block())
    result = callbacks.run(spec, engine, "SM89")
    assert result["steps"] == 8 and len(result["trace"]) == 8
    assert all(lane.state.load("ram", 16, 32) == 1 for lane in engine.lanes)


def test_shared_scheduler_capture_uses_block_local_lane_ids():
    body = ordered.Builder()
    lane = body.read_register("R", 0)
    local = body.read_register("R", 3)
    capture = ordered.Builder(body.count)
    source = capture.read_register("R", 1)
    snapshot, = capture.special(ordered.CAPTURE, lane, bitvec.const(3, 32), source)
    capture.emit(ordered.WriteRegister(("R", 1, 1), bitvec.const(99, 32)))
    value, = capture.special(ordered.READ_LANE, snapshot, lane ^ 1)
    capture.emit(ordered.WriteRegister(("R", 2, 1), value))
    body.emit(ordered.If(local < 2, capture.block()))
    spec = request(threads=66)
    spec["kernel"]["block_size"] = 33
    engine = Kernel(spec, body.block())
    result = callbacks.run(spec, engine, "SM89")
    assert result["steps"] == 132
    assert [engine.lanes[index].state.read("R", 2, 1) for index in (0, 1, 33, 34)] == [11, 10, 44, 43]
