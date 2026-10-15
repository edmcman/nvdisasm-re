"""GPU fixtures: their emulated outputs against the references (no GPU), and the references against
the SM89 GPU."""
import json
from functools import cache
from sass import ghidra_bridge
from sass.paths import ROOT
from sass.sleigh.gen import MODES
from tests.harness.gpu import Driver, Template, hits, mnemonic, request


PASSES = ROOT / "tests/baseline/gpu_passes.json"  # {fixture: {fingerprint, device}} of the last passing GPU run


def passes():
    return json.loads(PASSES.read_text()) if PASSES.exists() else {}


def passed(f):
    """Whether fixture f, exactly as it is now, has passed on the GPU."""
    return passes().get(f.name, {}).get("fingerprint") == f.fingerprint


@cache
def fixtures(families):
    from tests.cases import gpu_fixtures
    return gpu_fixtures(families)


def mismatches(got, want, same, excused=lambda i, g, w: False):
    return [i for i, (g, w) in enumerate(zip(got, want)) if not same(g, w) and not excused(i, g, w)]


def mismatch(f, side, got, bad):
    i = bad[0]
    return f"{side}/reference mismatch at thread {i} ({len(bad)} threads): inputs {f.vectors[i] if f.vectors else ''} {side} {got[i]:#x} reference {f.expected[i]:#x}"


def executed(f, visited):
    """Mnemonics (with modifiers) of the instructions the emulator visited."""
    t = Template(f.emulator_image or f.image, f.kernel)
    return {mnemonic(t.decoded(o)) for o in visited}


def emulated_problems(f, result):
    """Problems of one emulator result for fixture f, judged against its reference."""
    if f.expected is None: return ["no reference (draft fixture: derive one with tests.observe)"]
    if "error" in result: return [f"emulator error: {result['error']}"]
    out = [f"emulator never executed offsets {[hex(o) for o in missing]}"] if (missing := [o for o in f.visited if o not in result["visited"]]) else []
    if f.exit and result.get("exit") != "EXIT": out.append("emulator did not reach EXIT")
    if f.targets:
        ops = executed(f, result["visited"])
        out += [f"target {t} never executed" for t in f.targets if not any(hits(op, t) for op in ops)]
    emulated = ghidra_bridge.outputs(result)
    if len(emulated) != len(f.expected): out.append(f"emulator produced {len(emulated)} outputs, expected {len(f.expected)}")
    if bad := mismatches(emulated, f.expected, f.emulator_same or f.same, f.excused): out.append(mismatch(f, "emulator", emulated, bad))
    return out


def hardware_problems(f, gpu):
    """Problems of fixture f's GPU outputs, judged against its reference."""
    if f.expected is None: return ["no reference (draft fixture: derive one with tests.observe)"]
    return [mismatch(f, "gpu", gpu, bad)] if (bad := mismatches(gpu, f.expected, f.same)) else []


def emulate(fixtures, modes=MODES):
    """{(fixture name, mode): [problems]} for every fixture and each of its fp modes among `modes`."""
    selected = {mode: [f for f in fixtures if mode in f.modes] for mode in modes}
    results = ghidra_bridge.emulate_sharded({mode: ("SM89", mode, [request(f) for f in fs]) for mode, fs in selected.items()})
    return {(f.name, mode): emulated_problems(f, result) for mode, fs in selected.items() for f, result in zip(fs, results[mode])}


def run(fixtures):
    """{fixture name: [problems]} from running every fixture on the SM89 GPU, recording each
    passing fixture's fingerprint in PASSES (and dropping failing ones) for tests/evidence.py."""
    driver = Driver()
    try: found = {f.name: hardware_problems(f, driver.run(f)) for f in fixtures}
    finally: driver.close()
    record = passes()
    for f in fixtures:
        if found[f.name]: record.pop(f.name, None)
        else: record[f.name] = dict(fingerprint=f.fingerprint, device=driver.device)
    PASSES.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    return found
