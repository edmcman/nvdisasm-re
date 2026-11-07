import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).parents[2]))
import pyghidra
import sass.semantics.ordered
from sass.ghidra_bridge import ghidra_decode, ordered, userops
from sass.semantics import ordered_runtime
from tests.harness import ordered_gate


def run(ldefs):
    import jpype
    from ghidra.pcode.emu import PcodeEmulator
    from ghidra.pcode.exec import PcodeExecutorStatePiece
    from ghidra.program.model.pcode import Varnode

    language = ghidra_decode.load_language(ldefs, "OrderedGate:LE:64:default")
    reason = PcodeExecutorStatePiece.Reason.INSPECT
    space = language.getDefaultSpace()
    library_field = jpype.JClass("ghidra.pcode.emu.DefaultPcodeThread").class_.getDeclaredField("library")
    library_field.setAccessible(True)
    output = dict(registers=[], memory=[], pcode=[], predicates=[], record_leaks=False,
                  suspended_positions_retained=True, fault_atomic_released=False, ticket_released=False)

    def bytes_(value, size): return jpype.JArray(jpype.JByte)(value.to_bytes(size, "little"))
    def read(state, node): return int.from_bytes(bytes(state.getVar(node, reason)), "little")

    for mode in range(13):
        emulator = PcodeEmulator(language)
        thread = emulator.newThread()
        state = thread.getState()
        for name, value in (("R0", 0x3c003c00 if mode in (7, 9, 11) else 0 if mode == 4 else 7),
                            ("R1", 0x40004000 if mode in (7, 9, 11) else 10)):
            state.setVar(language.getRegister(name), bytes_(value, 4))
        state.setVar(language.getRegister("P0"), bytes_(1, 1))
        state.setVar(Varnode(space.getAddress(16), 4), bytes_(23, 4))
        state.setVar(Varnode(space.getAddress(0x1000), 2), bytes_(mode | 255 << 8, 2))
        thread.overrideCounter(space.getAddress(0x1000))
        ready, counts = [], []
        runtime = ordered_runtime.Runtime()

        def arrive(*args):
            counts.append("arrive")
            return runtime.retain(sass.semantics.ordered.TICKET, args),

        def wait(ticket):
            counts.append("wait")
            return sass.semantics.ordered.Pending(lambda: (3,) if ready else None)

        def wait_ready(ticket):
            counts.append("wait")
            return sass.semantics.ordered.Pending(lambda: () if ready else None)

        runtime.handlers = {
            "barrier_arrive": arrive,
            "barrier_wait": wait,
            "barrier_wait_ready": wait_ready,
            "warp_capture": lambda *args: (runtime.retain(sass.semantics.ordered.SNAPSHOT, {0: 10, 1: 11}),),
            "ordered_test_fail": lambda: (_ for _ in ()).throw(ValueError("gate failure")),
            "ordered_test_pair": lambda x: (x + 1, 1),
        }
        execution = ordered.Execution(thread, runtime)
        interrupts = lambda name: sass.semantics.ordered.OPERATIONS.get(
            name.removeprefix(ordered.PREFIX), ordered_gate.FAIL).suspends
        library, refs = userops.library(language, lambda name, args, size, dest: execution.call(name, args, dest), interrupts)
        library_field.set(thread, thread.getUseropLibrary().compose(library))
        status = execution.step()
        if mode in (3, 4):
            assert status.value == "suspended", str(execution.error)
            frame = thread.getFrame()
            position = int(frame.index())
            assert int(thread.getCounter().getOffset()) == 0x1000
            initial = read(state, language.getRegister("R0"))
            assert counts == ["arrive", "wait"]
            assert execution.step().value == "suspended"
            output["suspended_positions_retained"] &= int(thread.getFrame().index()) == position
            assert read(state, language.getRegister("R0")) == initial
            ready.append(True)
            status = execution.step()
            assert counts == ["arrive", "wait"]
        assert status.value == ("fault" if mode == 5 else "complete"), (mode, str(execution.error))
        if mode != 5: assert int(thread.getCounter().getOffset()) == 0x1001
        else:
            output["fault_atomic_released"] = runtime.owner is None
            assert int(thread.getCounter().getOffset()) == 0x1000
        output["registers"].append([read(state, language.getRegister(f"R{i}")) for i in range(6)])
        output["predicates"].append(read(state, language.getRegister("P0")))
        output["record_leaks"] |= any(type_ == ordered.RECORD for type_, _ in runtime.objects.values())
        if mode == 12: output["ticket_released"] = not runtime.objects
        output["memory"].append(read(state, Varnode(space.getAddress(16), 4)))
        instruction = ghidra_decode.parse(language, space.getAddress(0x1000), bytes([mode]))
        output["pcode"].append([str(op.getMnemonic()) for op in instruction.getPcode()])
    output["decompiled"] = decompile(language)
    output["cooperative_snapshots"] = snapshots(language, library_field, reason)
    output["cooperative_synchronization"] = synchronization(language, library_field, reason)
    output["block_barrier_pcode"] = {
        name: [str(operation.getMnemonic()) for operation in ghidra_decode.parse(
            language, space.getAddress(0x1000), bytes([mode])).getPcode()]
        for name, mode in (("implicit", 13), ("deferred", 14), ("deferred_wait", 15), ("exit", 16), ("warp", 17),
                           ("convergence", 18))}
    return output


def synchronization(language, library_field, reason):
    import jpype
    from ghidra.pcode.emu import PcodeEmulator
    from ghidra.program.model.pcode import Varnode
    from sass.emulate import machine
    from sass.semantics import runtime

    space = language.getDefaultSpace()
    library = machine.Library('#include "sass/abi.hpp"\n', "coordination").lib
    results = []
    for schedule in ("round_robin", "reverse", "seeded"):
        for protocol, modes in (("implicit", [13]), ("deferred", [14, 15]), ("exit", [16]), ("warp", [17]),
                                ("convergence", [18])):
            state = library.sass_coordination_new(2)
            emulator = PcodeEmulator(language)
            executions, runtimes, references, completed = [], [], [], [0, 0]
            try:
                for lane in range(2):
                    thread = emulator.newThread()
                    lane_state = thread.getState()
                    for name, value in (("R0", lane), ("R1", 7), ("R2", 9)):
                        lane_state.setVar(language.getRegister(name),
                                          jpype.JArray(jpype.JByte)(value.to_bytes(4, "little")))
                    lane_state.setVar(Varnode(space.getAddress(0x1000), len(modes)),
                                      jpype.JArray(jpype.JByte)(bytes(modes)))
                    thread.overrideCounter(space.getAddress(0x1000))
                    participant = machine.Coordination(library, state, lane)
                    provider = runtime.Runtime(89, {"coordination": participant})
                    operation_runtime = ordered_runtime.Runtime(provider=provider)
                    execution = ordered.Execution(thread, operation_runtime)
                    userop_library, refs = userops.library(
                        language, lambda name, args, size, output, execution=execution:
                        execution.call(name, args, output),
                        lambda name: sass.semantics.ordered.OPERATIONS[
                            name.removeprefix(ordered.PREFIX)].suspends)
                    references.extend(refs)
                    library_field.set(thread, thread.getUseropLibrary().compose(userop_library))
                    executions.append(execution)
                    runtimes.append(operation_runtime)
                rng = random.Random(0x5a55)
                for _ in range(6):
                    lanes = [0, 1]
                    match schedule:
                        case "reverse": lanes.reverse()
                        case "seeded": rng.shuffle(lanes)
                    for lane in lanes:
                        if completed[lane] == len(modes): continue
                        execution = executions[lane]
                        position = execution.position
                        status = execution.step()
                        assert status.value != "fault", str(execution.error)
                        if status.value == "suspended":
                            retained = execution.position
                            assert execution.step().value == "suspended"
                            assert execution.position == retained
                            if position is not None: assert retained == position
                        else:
                            completed[lane] += 1
                            if int(execution.thread.getCounter().getOffset()) == 0:
                                runtimes[lane].provider.context["coordination"].exit()
                            elif completed[lane] < len(modes): execution.status = None
                assert completed == [len(modes)] * 2
                def read(lane, name):
                    return int.from_bytes(bytes(executions[lane].thread.getState().getVar(
                        language.getRegister(name), reason)), "little")
                assert [read(lane, "R1") for lane in range(2)] == ([8, 7] if protocol == "exit" else [8, 8])
                if protocol == "deferred": assert [read(lane, "R2") for lane in range(2)] == [10, 10]
                assert all(not operation_runtime.objects for operation_runtime in runtimes)
                results.append(dict(schedule=schedule, protocol=protocol,
                                    results=[read(lane, "R3") for lane in range(2)]))
            finally:
                for operation_runtime in runtimes: operation_runtime.close()
                library.sass_coordination_free(state)
    return results


def snapshots(language, library_field, reason):
    import jpype
    from ghidra.pcode.emu import PcodeEmulator
    from ghidra.program.model.pcode import Varnode

    space = language.getDefaultSpace()
    results = []
    for schedule in ("round_robin", "reverse", "seeded"):
        emulator = PcodeEmulator(language)
        runtime = ordered_runtime.Cooperative()
        executions, refs = [], []
        for lane in range(2):
            thread = emulator.newThread()
            state = thread.getState()
            for name, value in (("R0", lane), ("R1", 10 + lane)):
                state.setVar(language.getRegister(name), jpype.JArray(jpype.JByte)(value.to_bytes(4, "little")))
            state.setVar(Varnode(space.getAddress(0x1000), 1), jpype.JArray(jpype.JByte)(b"\x04"))
            thread.overrideCounter(space.getAddress(0x1000))
            execution = ordered.Execution(thread, runtime)
            library, references = userops.library(
                language, lambda name, args, size, output, execution=execution: execution.call(name, args, output))
            refs.extend(references)
            library_field.set(thread, thread.getUseropLibrary().compose(library))
            executions.append(execution)
        rng = random.Random(0x5a55)
        for _ in range(4):
            lanes = [0, 1]
            match schedule:
                case "reverse": lanes.reverse()
                case "seeded": rng.shuffle(lanes)
            for lane in lanes:
                execution = executions[lane]
                runtime.bind(execution, lane, site=(0x1000, 0))
                assert execution.step().value != "fault", str(execution.error)
        assert all(execution.status.value == "complete" for execution in executions)
        values = [int.from_bytes(bytes(execution.thread.getState().getVar(language.getRegister("R2"), reason)), "little")
                  for execution in executions]
        results.append(values)
        runtime.close()
    return results


def decompile(language):
    from java.io import ByteArrayInputStream
    from java.lang import Object
    from ghidra.program.database import ProgramDB
    from ghidra.program.model.lang import CompilerSpecID
    from ghidra.program.model.address import AddressSet
    from ghidra.program.model.symbol import SourceType
    from ghidra.app.cmd.disassemble import DisassembleCommand
    from ghidra.app.decompiler import DecompInterface
    from ghidra.util.task import TaskMonitor
    import jpype

    consumer = Object()
    program = ProgramDB("ordered-gate", language, language.getCompilerSpecByID(CompilerSpecID("default")), consumer)
    transaction = program.startTransaction("gate")
    address = program.getAddressFactory().getDefaultAddressSpace().getAddress(0x1000)
    try:
        stream = ByteArrayInputStream(jpype.JArray(jpype.JByte)(bytes([2, 255])))
        program.getMemory().createInitializedBlock("code", address, stream, 2, TaskMonitor.DUMMY, False)
        body = AddressSet(address, address.add(1))
        assert DisassembleCommand(address, body, True).applyTo(program)
        function = program.getFunctionManager().createFunction("atomic_gate", address, body, SourceType.USER_DEFINED)
    finally: program.endTransaction(transaction, True)
    decompiler = DecompInterface()
    try:
        assert decompiler.openProgram(program)
        result = decompiler.decompileFunction(function, 30, TaskMonitor.DUMMY)
        assert result.decompileCompleted(), str(result.getErrorMessage())
        return str(result.getDecompiledFunction().getC())
    finally:
        decompiler.dispose()
        program.release(consumer)


if __name__ == "__main__":
    pyghidra.start(install_dir=ghidra_decode.GHIDRA)
    print(json.dumps(run(sys.argv[1])))
