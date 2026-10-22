"""The Ghidra result caches' key (`ghidra_bridge.sources`) covers every module the Ghidra-side
emulator loads, and excludes generators only because the emulator never loads them."""
import json, subprocess
from pathlib import Path
import pytest
from sass import ghidra_bridge
from sass.paths import GHIDRA_PY, LDEFS, language
from tests.cases import emulator_cases
from tests.harness import hardware
from tests.harness.select import arches, require_languages

PROBE = """
import atexit, runpy, sys
atexit.register(lambda: print("MODULES", *sys.modules, file=sys.stderr))
sys.argv = sys.argv[1:]
sys.path.insert(0, sys.argv[0].rsplit("/", 1)[0])
runpy.run_path(sys.argv[0], run_name="__main__")
"""


@pytest.mark.ghidra
def test_ghidra_programs_skip_generators(request):
    """Single instructions, runtime CALLOTHERs (MUFU), launch kernels and cooperative warp
    collectives (operand spans from family.roles)."""
    if "SM89" not in arches(request.config): pytest.skip("SM89 launch kernels: --arch does not select SM89")
    require_languages([("SM89", "exact")])
    cases = emulator_cases("SM89", "exact", ("control", "mufu"), 0, generated=False)
    requests = [c.request for family in cases.values() for c in family] + [hardware.request(f) for _, f in hardware.fixtures(("threads",))]
    program = Path(ghidra_bridge.__file__).parent / "ghidra_emulate.py"
    p = subprocess.run([GHIDRA_PY, "-c", PROBE, str(program), str(LDEFS), language("SM89", "exact")],
                       input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True, cwd="/")
    assert p.returncode == 0 and sum(line.startswith("{") for line in p.stdout.splitlines()) == len(requests), p.stderr[-3000:]
    loaded = next(line for line in p.stderr.splitlines() if line.startswith("MODULES ")).split()[1:]
    modules = [m for m in loaded if m.startswith("sass.")] + [f"{ghidra_bridge.__name__}.{m}" for m in loaded if ghidra_bridge.module_file(f"{ghidra_bridge.__name__}.{m}")]
    assert not [m for m in modules if ghidra_bridge.generator(m)]
    assert {ghidra_bridge.module_file(m) for m in modules} - {None} <= set(ghidra_bridge.sources(program.name))
