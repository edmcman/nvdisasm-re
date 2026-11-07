import subprocess, sys
import pytest
from sass.assemble import assemble_cubin
from sass.paths import ROOT

EXAMPLES = ROOT / "examples"


def run(script, *args): return subprocess.run([sys.executable, EXAMPLES / script, *args], capture_output=True, text=True, check=True).stdout


def test_lift_cubin(tmp_path):
    cubin = tmp_path / "write_value.cubin"
    cubin.write_bytes(assemble_cubin((EXAMPLES / "assembler/write_value.sass").read_text()))
    out = run("lift_cubin.py", str(cubin))
    assert "SM89 .text.write_value" in out and "space='cbank', address=0x168:64" in out
    assert "WriteRegister(location=('R', 5, 1)" in out and "Transfer(kind='return'" in out
    assert "STG.E [R2.64], R5" in out and "WriteMemory(space='ram'" in out and "undecodable" not in out


def test_assemble_and_lift():
    lines = run("assemble_and_lift.py").splitlines()
    assert lines[:2] == [
        "23720001020000000300000000c00f00 FFMA R0, R1, R2, R3 ffma__RRR_RRR", "native "]
    assert [line.split("location=")[1] for line in lines[2:5]] == [
        "('R', 1, 1))", "('R', 2, 1))", "('R', 3, 1))"]
    assert lines[-1].startswith("  WriteRegister(location=('R', 0, 1)")


def test_evaluate(): assert run("evaluate.py") == "FFMA R0, R1, R2, R3 -> 3.25 next pc 0x110\n"


@pytest.mark.native
def test_run_kernel():
    assert run("emulator/run_kernel.py") == "4096 threads, 49152 steps; out[1] = 0x673115e0 = 3 * 0x2265b1f5 + 1: True\nthe Python engine agrees: True\n"


@pytest.mark.native
def test_equivalence():
    assert run("emulator/equivalence.py") == "times3 == added: True\ntimes3 vs buggy: 8192 differing threads; launch 0 thread 0: 68178488 vs 67178488\n"


@pytest.mark.native
def test_trace_kernel():
    out = run("emulator/trace_kernel.py").splitlines()
    trace = [line for line in out if line.startswith("thread 0 0x")]
    assert trace[9] == "thread 0 0x0090 IADD3 R5, R5, 0x1, RZ                    R5=0x10" and trace[10] == "thread 0 0x00a0 STG.E [R8.64], R5"
    assert out[-4:] == ["thread 0: in 0x00000005 -> out 0x00000010", "thread 1: in 0x7fffffff -> out 0x7ffffffe",
                        "thread 2: in 0x000003e8 -> out 0x00000bb9", "thread 3: in 0xffffffff -> out 0xfffffffe"]


@pytest.mark.native
def test_show_cpp(tmp_path):
    out = run("emulator/show_cpp.py", "buggy")
    body = out.split("SASS_HD static void i10(sass::Ctx &c) {", 1)[1].split("SASS_HD static void i11", 1)[0]
    assert body.index("t.p[0]") < body.index("if (q0)") < body.index("sass::u64(t.r[5])") < body.index("t.r[5] =")
    assert "goto ordered_5;" in body and "t.pc = UINT64_C(1048752);" in body
    assert run("emulator/show_cpp.py", "times3", str(tmp_path / "t.cpp")).startswith("times3: 16 instructions")
    assert 'extern "C" uint32_t sass_run(' in (tmp_path / "t.cpp").read_text()
