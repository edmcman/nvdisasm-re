import subprocess, sys
from sass.assemble import assemble_cubin
from sass.paths import ROOT

EXAMPLES = ROOT / "examples"


def run(script, *args): return subprocess.run([sys.executable, EXAMPLES / script, *args], capture_output=True, text=True, check=True).stdout


def test_lift_cubin(tmp_path):
    cubin = tmp_path / "write_value.cubin"
    cubin.write_bytes(assemble_cubin((EXAMPLES / "assembler/write_value.sass").read_text()))
    out = run("lift_cubin.py", str(cubin))
    assert "SM89 .text.write_value" in out and "R5 <- cbank[0x168:64]:32" in out and "flow return" in out
    assert "STG.E [R2.64], R5" in out and "ram[R2:R3] <- R5" in out and "undecodable" not in out


def test_assemble_and_lift():
    assert run("assemble_and_lift.py").splitlines() == [
        "23720001020000000300000000c00f00 FFMA R0, R1, R2, R3 ffma__RRR_RRR", "native ", "  R0 <- nanresult(ffma(R1, R2, R3, 'RN'), 0x7fffffff)"]


def test_evaluate(): assert run("evaluate.py") == "FFMA R0, R1, R2, R3 -> 3.25 next pc 0x110\n"
