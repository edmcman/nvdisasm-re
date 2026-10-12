"""Layer 5: run GPU fixtures on the SM89 device and in the emulator, and report disagreements."""
from tests.harness import ghidra
from tests.harness.gpu import Driver, request
from tests.paths import FP_MODES


def first_mismatch(got, want, same):
    return next((i for i, (g, w) in enumerate(zip(got, want)) if not same(g, w)), None)


def executed(f, visited):
    """Mnemonics (with modifiers) of the instructions the emulator visited."""
    from tests.harness.gpu import Template
    t = Template(f.emulator_image or f.image, f.kernel)
    return {t.decoded(o).text.split()[0] if not t.decoded(o).text.startswith("@") else t.decoded(o).text.split()[1] for o in visited}


def check(f, gpu, result):
    """Problems for one fixture given its GPU outputs and one emulator result."""
    out = []
    if f.expected is not None and (i := first_mismatch(gpu, f.expected, f.same)) is not None:
        out.append(f"GPU/reference mismatch at thread {i}: inputs {f.vectors[i] if f.vectors else ''} gpu {gpu[i]:#x} reference {f.expected[i]:#x}")
    if "error" in result: return out + [f"emulator error: {result['error']}"]
    missing = [o for o in f.visited if o not in result["visited"]]
    if missing: out.append(f"emulator never executed offsets {[hex(o) for o in missing]}")
    if f.exit and result.get("exit") != "EXIT": out.append("emulator did not reach EXIT")
    if f.targets:
        ops = executed(f, result["visited"])
        out += [f"target {t} never executed" for t in f.targets if not any(op == t or op.startswith(t + ".") for op in ops)]
    emulated = ghidra.outputs(result)
    other = f.expected if f.emulator_vs == "reference" else gpu
    same = f.emulator_same or f.same
    bad = [i for i, (e, o) in enumerate(zip(emulated, other)) if not same(e, o) and not f.excused(i, e, o)]
    if len(emulated) != len(other): out.append(f"emulator produced {len(emulated)} outputs, expected {len(other)}")
    if bad: out.append(f"emulator/{f.emulator_vs} mismatch at thread {bad[0]} ({len(bad)} threads): emulated {emulated[bad[0]]:#x} {f.emulator_vs} {other[bad[0]]:#x}")
    return out


def run(fixtures, modes=FP_MODES):
    """{(fixture name, mode): [problems]} for every fixture and each of its fp modes."""
    driver = Driver()
    try: gpu = {f.name: driver.run(f) for f in fixtures}
    finally: driver.close()
    jobs = {mode: ("SM89", mode, [request(f) for f in fixtures if mode in f.modes]) for mode in modes}
    results = ghidra.concurrently(ghidra.emulate, jobs)
    out = {}
    for mode in modes:
        for f, result in zip((f for f in fixtures if mode in f.modes), results[mode]):
            out[f.name, mode] = check(f, gpu[f.name], result)
    return out
