import pytest
from sass.assemble import assemble_instruction
from sass.decode import ir
from sass.emulate import callbacks, codegen, kernel, reference
from sass.semantics import bitvec, lifter, ordered
from tests.harness import ordered_gate


pytestmark = pytest.mark.native


@pytest.mark.parametrize("deferred", [False, True])
def test_native_memory_fast_path_requires_proof_of_empty_deferred_barriers(deferred):
    texts = (["BAR.SYNC.DEFER_BLOCKING 0"] if deferred else []) + ["LDS R1, [R0]", "EXIT"]
    code = b"".join(assemble_instruction("SM89", text) for text in texts)
    source, instructions = codegen.kernel("SM89", code, 0x1000)
    memory = instructions[-2].semantics
    assert any(isinstance(statement, ordered.Special) and statement.operation == ordered.BLOCK_WAIT_DEFERRED
               for statement in ordered.linearize(memory.body))
    assert ("runtime.block_barrier_wait_deferred()" in source) == deferred
    assert ("static const sass::OrderedStep ORDERED" in source) == deferred
    request = dict(kernel=dict(code=code.hex(), address=0x1000, threads=2, block_size=2, scope="launch",
                               trace=True, trace_threads=2),
                   memory=[dict(space="shared", address=0, hex="2a000000")], output=dict(address=0x2000))
    actual = kernel.run_kernel(request, "SM89")
    assert actual == reference.run_kernel(request, "SM89")
    assert [entry["writes"]["R1"] for entry in actual["trace"] if "R1" in entry["writes"]] == [42, 42]


def test_native_specialization_cannot_hide_an_invalid_atomic_wait(monkeypatch):
    actual_lift = lifter.lift

    def lift(*args, **kwargs):
        semantics = actual_lift(*args, **kwargs)
        semantics.body = ordered.Block((ordered.Atomic(ordered.Block((
            ordered.Special(ordered.BLOCK_WAIT_DEFERRED),))),))
        return semantics

    monkeypatch.setattr(lifter, "lift", lift)
    with pytest.raises(TypeError, match="suspension"):
        codegen.kernel("SM89", assemble_instruction("SM89", "NOP"), 0x1000)


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
@pytest.mark.parametrize("protocol", ["warp", "convergence"])
def test_warp_synchronization_keeps_effects_across_native_and_callback_suspension(monkeypatch, arch, schedule, protocol):
    actual_lift = lifter.lift

    def lift(arch, word, address=0, fp="exact", **kwargs):
        semantics = actual_lift(arch, word, address, fp, **kwargs)
        if address == 0x1000:
            semantics.body = ordered_gate.warp_sync_body() if protocol == "warp" else ordered_gate.convergence_body()
        return semantics

    monkeypatch.setattr(lifter, "lift", lift)
    code = b"".join(assemble_instruction(arch, text) for text in ("NOP", "EXIT"))
    request = dict(kernel=dict(code=code.hex(), address=0x1000, threads=2, block_size=2, scope="launch",
                               schedule=schedule, trace=True, trace_threads=2, max_steps=2,
                               registers={"R1": 7}), output=dict(address=0x2000))
    expected = reference.run_kernel(request, arch)
    assert expected["steps"] == 4
    assert kernel.Kernel(arch, code, 0x1000).run(request) == expected
    visits = [entry for entry in expected["trace"] if entry["pc"] == 0]
    assert len(visits) == 2 and all(entry["writes"] == {"R1": 8} for entry in visits)


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
@pytest.mark.parametrize("protocol", ["implicit", "counted", "deferred", "exit"])
def test_shared_block_barrier_ordered_bodies(monkeypatch, arch, schedule, protocol):
    count = 64 if protocol == "counted" else 2
    bodies = {0x1000: ordered_gate.block_barrier_body(
        deferred=protocol == "deferred", counted=count if protocol == "counted" else 0,
        exiting=protocol == "exit",
        lane_location=("SR", ir.load(arch).enums["SpecialRegister"]["SR_LANEID"]), exit_target=0x1020)}
    if protocol == "deferred": bodies[0x1010] = ordered_gate.block_barrier_deferred_wait_body()
    actual_lift = lifter.lift

    def lift(arch, word, address=0, fp="exact", **kwargs):
        semantics = actual_lift(arch, word, address, fp, **kwargs)
        if address in bodies: semantics.body = bodies[address]
        return semantics

    monkeypatch.setattr(lifter, "lift", lift)
    code = b"".join(assemble_instruction(arch, text) for text in ("NOP", "NOP", "EXIT"))
    request = dict(kernel=dict(code=code.hex(), address=0x1000, threads=count, block_size=count, scope="launch",
                               schedule=schedule, trace=True, trace_threads=count, max_steps=3,
                               registers={"R1": 7, "R2": 9}),
                   output=dict(address=0x2000))
    expected = reference.run_kernel(request, arch)
    actual = kernel.Kernel(arch, code, 0x1000).run(request)
    assert actual == expected
    assert actual["steps"] == (5 if protocol == "exit" else count * 3)
    visits = [entry for entry in actual["trace"] if entry["pc"] == 0 and entry["thread"] == 0]
    assert len(visits) == 1 and visits[0]["writes"]["R1"] == 8
    pc = 16 if protocol == "deferred" else 0
    results = [entry["writes"]["R3"] for entry in actual["trace"]
               if entry["pc"] == pc and "R3" in entry["writes"] and
               (protocol != "exit" or entry["thread"] == 0)]
    assert results == [1 if protocol == "exit" else count] * (1 if protocol == "exit" else count)
    if protocol == "deferred":
        assert [entry["writes"]["R2"] for entry in actual["trace"] if entry["pc"] == 16] == [10] * count


def collective_request(arch, schedule, opcode):
    initialization = ["S2R R1, SR_TID.X", "LOP3.LUT R1, R1, 1, RZ, 0xc0, !PT",
                      "ISETP.NE.AND P1, PT, R1, RZ, PT", "MOV R0, 7", "MOV R3, 3"]
    operation = {"vote": "VOTE.ANY R1, P1, P1", "match": "MATCH.ANY.U64 R1, R0",
                 "redux": "REDUX.SUM UR1, R1"}[opcode]
    pc = 0x1000 + 16 * len(initialization)
    texts = initialization + [operation, "MOV R1, UR1" if opcode == "redux" else "NOP",
                              "IADD3 R3, R3, -1, RZ", "ISETP.GT.AND P0, PT, R3, 0, PT",
                              f"@P0 BRA {pc:#x}", "S2R R2, SR_TID.X", "IMAD R2, R2, 4, RZ",
                              "IADD3 R2, R2, 0x2000, RZ", "STG [R2], R1", "EXIT"]
    code = b"".join(assemble_instruction(arch, text, 0x1000 + 16 * index) for index, text in enumerate(texts))
    return dict(kernel=dict(code=code.hex(), address=0x1000, threads=64, block_size=64, scope="launch",
                            schedule=schedule, trace=True, trace_threads=64, max_steps=40),
                output=dict(address=0x2000))


def check_collective(result, opcode):
    expected = {"vote": [0xffffffff] * 64, "match": [0xaaaaaaaa if index & 1 else 0x55555555 for index in range(64)],
                "redux": [16 * 32 * 32] * 64}[opcode]
    assert result["outputs"] == [value.to_bytes(4, "little").hex() for value in expected]
    visits = [entry for entry in result["trace"] if entry["pc"] == 80]
    assert len(visits) == 3 * 64
    if opcode == "vote":
        first = {}
        for entry in visits: first.setdefault(entry["thread"], entry["writes"]["R1"])
        assert list(first.values()) == [0xaaaaaaaa] * 64


@pytest.mark.usefixtures("selected_architecture")
@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
@pytest.mark.parametrize("opcode", ["vote", "match", "redux"])
def test_ordered_collectives_capture_aliases_reuse_sites_and_release_handles(arch, schedule, opcode):
    request = collective_request(arch, schedule, opcode)
    class TrackingKernel(reference.Kernel):
        def group(self, threads, scope):
            group = super().group(threads, scope)
            self.groups.append(group)
            return group

    engine = TrackingKernel(request, arch)
    engine.groups = []
    expected = callbacks.run(request, engine, arch)
    check_collective(expected, opcode)
    assert all(not lane.ordered_runtime.objects for group in engine.groups for lane in group)
    assert kernel.run_kernel(request, arch) == expected


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_ordered_collective_kernels_ghidra(arch, pytestconfig):
    from sass import ghidra_bridge
    from tests.harness import select
    if arch not in select.arches(pytestconfig): pytest.skip(f"{arch} not selected")
    modes = select.modes(pytestconfig)
    select.require_languages([(arch, mode) for mode in modes])
    inputs = [(opcode, collective_request(arch, schedule, opcode))
              for opcode in ("vote", "match", "redux") for schedule in ("round_robin", "reverse", "seeded")]
    expected = [reference.run_kernel(request, arch) for _, request in inputs]
    for (opcode, _), result in zip(inputs, expected): check_collective(result, opcode)
    found = ghidra_bridge.emulate_sharded({mode: (arch, mode, [request for _, request in inputs])
                                         for mode in modes}, workers=len(modes))
    assert all(results == expected for results in found.values())


def shuffle_request(arch, schedule):
    texts = ["S2R R1, SR_TID.X", "MOV R3, 3", "SHFL.BFLY PT, R1, R1, 1, 31",
             "IADD3 R3, R3, -1, RZ", "ISETP.GT.AND P0, PT, R3, 0, PT", "@P0 BRA 0x1020",
             "S2R R2, SR_TID.X", "IMAD R2, R2, 4, RZ", "IADD3 R2, R2, 0x2000, RZ",
             "STG [R2], R1", "EXIT"]
    code = b"".join(assemble_instruction(arch, text, 0x1000 + 16 * index) for index, text in enumerate(texts))
    return dict(kernel=dict(code=code.hex(), address=0x1000, threads=64, block_size=64,
                            scope="launch", schedule=schedule, max_steps=32),
                output=dict(address=0x2000))


@pytest.mark.parametrize("arch", ["SM89", "SM90"])
@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
def test_aliased_shfl_reuses_its_site_and_releases_snapshots(arch, schedule):
    request = shuffle_request(arch, schedule)
    class TrackingKernel(reference.Kernel):
        def group(self, threads, scope):
            group = super().group(threads, scope)
            self.groups.append(group)
            return group

    engine = TrackingKernel(request, arch)
    engine.groups = []
    expected = callbacks.run(request, engine, "SM89")
    assert expected["outputs"] == [(index ^ 1).to_bytes(4, "little").hex() for index in range(64)]
    assert all(not lane.ordered_runtime.objects for group in engine.groups for lane in group)
    assert kernel.run_kernel(request, arch) == expected


@pytest.mark.ghidra
@pytest.mark.parametrize("arch", ["SM89", "SM90"])
def test_aliased_shfl_ghidra_resumes_without_metadata_capture(arch, pytestconfig):
    from sass import ghidra_bridge
    from tests.harness import select
    if arch not in select.arches(pytestconfig): pytest.skip(f"{arch} not selected")
    modes = select.modes(pytestconfig)
    select.require_languages([(arch, mode) for mode in modes])
    requests = [shuffle_request(arch, schedule) for schedule in ("round_robin", "reverse", "seeded")]
    expected = [reference.run_kernel(request, arch) for request in requests]
    assert all(result["outputs"] == [(index ^ 1).to_bytes(4, "little").hex() for index in range(64)]
               for result in expected)
    found = ghidra_bridge.emulate_sharded({mode: (arch, mode, requests) for mode in modes}, workers=len(modes))
    assert all(results == expected for results in found.values())


@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
def test_native_kernel_retains_snapshots_and_effects_across_suspension(monkeypatch, schedule):
    body = ordered.Builder()
    lane = body.read_register("SR", ir.load("SM89").enums["SpecialRegister"]["SR_LANEID"])
    original = body.move(lane + 10)
    snapshot, = body.special(ordered.CAPTURE, lane, bitvec.const(3, 32), original)
    before = body.read_register("R", 1)
    body.emit(ordered.WriteRegister(("R", 1, 1), before + 1))
    ticket, = body.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(2, 32), bitvec.const(1, 1))
    body.special(ordered.WAIT_READY, ticket)
    other, = body.special(ordered.READ_LANE, snapshot, lane ^ 1)
    body.emit(ordered.WriteRegister(("R", 2, 1), other))
    actual_lift = lifter.lift

    def lift(arch, word, address=0, fp="exact", **kwargs):
        semantics = actual_lift(arch, word, address, fp, **kwargs)
        if address == 0x1000: semantics.body = body.block()
        return semantics
    monkeypatch.setattr(lifter, "lift", lift)
    code = b"".join(assemble_instruction("SM89", text) for text in ("NOP", "EXIT"))
    program = kernel.Kernel("SM89", code, 0x1000)
    request = dict(kernel=dict(code=code.hex(), address=0x1000, threads=2, block_size=2, scope="launch",
                               schedule=schedule, trace=True, trace_threads=2, max_steps=2, registers={"R1": 7}),
                   output=dict(address=0x2000))
    result = program.run(request)
    assert result["steps"] == 4
    assert sorted((entry["thread"], entry["writes"]) for entry in result["trace"] if entry["pc"] == 0) == [
        (0, {"R1": 8, "R2": 11}), (1, {"R1": 8, "R2": 10})]
    assert len(result["trace"]) == 4


@pytest.mark.parametrize("schedule", ["round_robin", "reverse", "seeded"])
@pytest.mark.parametrize("protocol", ["abstract", "block"])
def test_native_ordinary_arrival_shares_runtime_with_a_later_wait(monkeypatch, schedule, protocol):
    bodies = []
    for participant, result in ((1, 8), (0, 7)):
        body = ordered.Builder()
        lane = body.read_register("SR", ir.load("SM89").enums["SpecialRegister"]["SR_LANEID"])
        arm = ordered.Builder(body.count)
        operation, arguments, wait, release = (
            (ordered.BLOCK_ARRIVE_IMPLICIT, (bitvec.const(0, 32), bitvec.const(participant, 1),
                                            bitvec.const(3, 32)), ordered.BLOCK_WAIT_READY, ordered.BLOCK_RELEASE)
            if protocol == "block" else
            (ordered.ARRIVE, (bitvec.const(0, 32), bitvec.const(2, 32),
                              bitvec.const(participant, 1)), ordered.WAIT_READY, ordered.RELEASE_TICKET))
        ticket, = arm.special(operation, *arguments)
        if participant == 0: arm.special(wait, ticket)
        arm.special(release, ticket)
        arm.emit(ordered.WriteRegister(("R", 2, 1), bitvec.const(result, 32)))
        body.emit(ordered.If(lane == participant, arm.block()))
        bodies.append(body.block())
    actual_lift = lifter.lift

    def lift(arch, word, address=0, fp="exact", **kwargs):
        semantics = actual_lift(arch, word, address, fp, **kwargs)
        if address in (0x1000, 0x1010): semantics.body = bodies[(address - 0x1000) // 16]
        return semantics

    monkeypatch.setattr(lifter, "lift", lift)
    code = b"".join(assemble_instruction("SM89", text) for text in ("NOP", "NOP", "EXIT"))
    program = kernel.Kernel("SM89", code, 0x1000)
    request = dict(kernel=dict(code=code.hex(), address=0x1000, threads=2, block_size=2, scope="launch",
                               schedule=schedule, trace=True, trace_threads=2, max_steps=3),
                   output=dict(address=0x2000))
    result = program.run(request)
    assert result == reference.run_kernel(request, "SM89")
    assert result["steps"] == 6 and len(result["trace"]) == 6
    assert {(entry["thread"], entry["pc"], entry["writes"]["R2"])
            for entry in result["trace"] if entry["pc"] < 32} == {
        (0, 0, 0), (1, 0, 8), (0, 16, 7), (1, 16, 8)}


def test_native_ordinary_runtime_path_has_no_resumption_frame(monkeypatch):
    from sass.emulate import codegen
    body = ordered.Builder()
    ticket, = body.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(1, 32), bitvec.const(1, 1))
    body.special(ordered.RELEASE_TICKET, ticket)
    body.emit(ordered.WriteRegister(("R", 2, 1), bitvec.const(7, 32)))
    actual_lift = lifter.lift

    def lift(arch, word, address=0, fp="exact", **kwargs):
        semantics = actual_lift(arch, word, address, fp, **kwargs)
        if address == 0x1000: semantics.body = body.block()
        return semantics

    monkeypatch.setattr(lifter, "lift", lift)
    code = b"".join(assemble_instruction("SM89", text) for text in ("NOP", "EXIT"))
    source, _ = codegen.kernel("SM89", code, 0x1000)
    assert "OrderedOrdinaryStep ORDINARY[]" in source
    assert "sass::ordered::Frame" not in source and "OrderedStep ORDERED[]" not in source
    assert "sass::run<true, false, false>" in source
    fragment = codegen.fragments([("SM89", code[:16], 0x1000)])
    assert "sass::ordered::Frame" not in fragment
    program = kernel.Kernel("SM89", code, 0x1000)
    request = dict(kernel=dict(code=code.hex(), address=0x1000, threads=2, block_size=2, scope="launch",
                               trace=True, trace_threads=2, max_steps=2), output=dict(address=0x2000))
    result = program.run(request)
    assert result["steps"] == 4
    assert [entry["writes"] for entry in result["trace"] if entry["pc"] == 0] == [{"R2": 7}, {"R2": 7}]
