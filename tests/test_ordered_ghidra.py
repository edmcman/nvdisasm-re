import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import zlib
import pytest
from sass.paths import GHIDRA_PY
from sass.sleigh import build, gen, lower, ordered
from tests.harness import ordered_gate


pytestmark = pytest.mark.ghidra


def language(directory, fp):
    generator = gen.Gen("SM89", fp)
    builder = SimpleNamespace(g=generator, refs=[], serial=0)
    bodies = [ordered_gate.register_body(), ordered_gate.branch_body(True), ordered_gate.atomic_body(),
              ordered_gate.suspension_body(), ordered_gate.snapshot_body(), ordered_gate.atomic_body(True),
              ordered_gate.pair_body(), ordered_gate.numerical_body(), ordered_gate.repeated_choice_body(),
              ordered_gate.numerical_body(True), ordered_gate.wide_register_body(),
              ordered_gate.numerical_outputs_body(), ordered_gate.ticket_release_body(),
              ordered_gate.block_barrier_body(), ordered_gate.block_barrier_body(deferred=True),
              ordered_gate.block_barrier_deferred_wait_body(), ordered_gate.block_barrier_body(exiting=True),
              ordered_gate.warp_sync_body(), ordered_gate.convergence_body()]
    codes = [ordered.emit(ordered.normalize(body), builder) for body in bodies]
    constructors = [f":GATE{index} is opcode={index}" + "".join(f" & {name}" for _, name in sorted(code.refs)) +
                    f" {{ {code.body()} }}" for index, code in enumerate(codes)]
    lower.shared_tables(generator)
    spec = """
define endian=little;
define alignment=1;
define space ram type=ram_space size=8 default;
define space shared type=ram_space size=8;
define space localmem type=ram_space size=8;
define space cbank type=ram_space size=8;
define space register type=register_space size=8;
define register offset=0 size=4 [ R0 R1 R2 R3 R4 R5 R6 R7 ];
define register offset=32 size=1 [ P0 P1 P2 P3 P4 P5 P6 P7 ];
define register offset=40 size=8 [ PC ];
define token gate_word(8) opcode=(0,7);
"""
    spec += "\n".join(f"define pcodeop {name};" for name in sorted(generator.pcodeops))
    offset = 128
    for name, size in generator.temporaries.items():
        spec += f"\ndefine register offset={offset} size={size} [ {name} ];"
        offset += size
    spec += "\n" + "\n".join(line.replace("(lany ; hany)", "opcode")
                             for line in gen.reachable_tables(generator.table_lines, constructors))
    spec += "\n" + "\n".join(constructors) + "\n:EXIT is opcode=255 { return [0:8]; }\n"
    (directory / "gate.slaspec").write_text(spec)
    (directory / "gate.pspec").write_text('<processor_spec><programcounter register="PC"/></processor_spec>')
    (directory / "gate.cspec").write_text("""
<compiler_spec>
  <data_organization><pointer_size value="8"/></data_organization>
  <global><range space="ram"/><range space="shared"/></global>
  <stackpointer register="R7" space="localmem"/>
  <default_proto><prototype name="gate" extrapop="0" stackshift="0"><input/><output/></prototype></default_proto>
</compiler_spec>
""")
    (directory / "gate.ldefs").write_text("""
<language_definitions>
  <language processor="OrderedGate" endian="little" size="64" variant="default" version="1.0"
            slafile="gate.sla" processorspec="gate.pspec" id="OrderedGate:LE:64:default">
    <description>Ordered semantics feasibility gate</description>
    <compiler name="default" spec="gate.cspec" id="default"/>
  </language>
</language_definitions>
""")
    return spec


@pytest.mark.parametrize("fp", ["exact", "readable"])
def test_ordered_ghidra_gate(tmp_path, fp):
    language(tmp_path, fp)
    install = Path(os.environ.get("GHIDRA_INSTALL_DIR", str(Path.home() / "Ghidra/ghidra_12.2_PUBLIC")))
    compile_ = subprocess.run([str(install / "support/sleigh"), "-t", str(tmp_path / "gate.slaspec")],
                              capture_output=True, text=True,
                              env={**os.environ, "JAVA_TOOL_OPTIONS": "-Xmx2g"})
    assert compile_.returncode == 0, compile_.stdout + compile_.stderr
    size = len(zlib.decompress((tmp_path / "gate.sla").read_bytes()[4:]))
    assert size < build.LIMIT
    script = Path(__file__).with_name("harness") / "ordered_ghidra.py"
    run = subprocess.run([GHIDRA_PY, str(script), str(tmp_path / "gate.ldefs")],
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    results = json.loads(run.stdout.splitlines()[-1])
    results["decompressed_bytes"] = size
    results["fp"] = fp
    (tmp_path / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    assert results["registers"][0][:2] == [8, 8]
    assert results["registers"][1][0] == 23
    assert results["memory"][2] == 24
    assert results["registers"][3][:2] == [8, 10]
    assert results["registers"][4][1:3] == [99, 11]
    assert results["memory"][5] == 24
    assert results["registers"][6][0] == 6
    assert results["predicates"][6] == 1
    assert results["registers"][7][2] == 0x42004200
    assert results["registers"][8][1:3] == [100, 17]
    assert results["registers"][9][2] == 0x42004200
    assert results["registers"][10][2:6] == [4, 3, 7, 10]
    assert results["registers"][11][2:4] == [0x42004200, 0x42004201]
    assert results["registers"][12][0] == 42 and results["ticket_released"]
    assert len(results["pcode"][11]) <= len(results["pcode"][7]) + 4
    assert not results["record_leaks"]
    assert results["cooperative_snapshots"] == [[11, 10], [11, 10], [11, 10]]
    assert results["cooperative_synchronization"] == [
        {"schedule": schedule, "protocol": protocol,
         "results": [1, 0] if protocol == "exit" else [0, 0] if protocol in ("warp", "convergence") else [2, 2]}
        for schedule in ("round_robin", "reverse", "seeded")
        for protocol in ("implicit", "deferred", "exit", "warp", "convergence")]
    assert {name: operations.count("CALLOTHER") for name, operations in
            results["block_barrier_pcode"].items()} == {
                "implicit": 4, "deferred": 3, "deferred_wait": 2, "exit": 4, "warp": 3, "convergence": 4}
    assert results["fault_atomic_released"]
    assert results["suspended_positions_retained"]
    assert "LOAD" in results["pcode"][2] and "STORE" in results["pcode"][2]
    assert results["pcode"][2].count("CALLOTHER") == 2
    assert "sass_ordered_atomic_enter" in results["decompiled"]
    assert "sass_ordered_atomic_exit" in results["decompiled"]
    assert "iRam0000000000000010 = iRam0000000000000010 + 1;" in results["decompiled"]
