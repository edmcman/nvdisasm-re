"""Per-class verification evidence, derived from the tests: SM89 GPU fixtures' data paths (`gpu`),
annotated `tests/ptx` probes compiled by ptxas (`ptxas`) and emulator cases checked against
`tests/refs` (`reference`). Writes sass_smXX_evidence.json beside the coverage manifests, which
`sass-coverage` folds into processor/SASS/semantics/evidence.json and the report.

usage: python -m tests.evidence [SMxx ...] [--output DIR]
"""
import argparse, json, os, re, subprocess, tempfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from functools import cache
from pathlib import Path
from sass.decode import cubin, pydecode
from sass.paths import LANGUAGES, PTXAS, ROOT
from sass.sleigh.build import ARCHES
from sass.sleigh.gen import MODES, Gen
from tests.cases import emulator_cases, gpu_fixtures
from tests.harness.dataflow import expected
from tests.harness.gpu import Template, hits, mnemonic
from tests.harness.hardware import passed

PROBES = ROOT / "tests/ptx"


def decoded(arch, code):
    """(offset, Decoded) for the decodable instruction words of `code`."""
    for o in range(0, len(code) - 15, 16):
        try: yield o, pydecode.decode(arch, code[o:o + 16])
        except Exception: pass


def reference(arch):
    """{class: case count} over both fp lowerings."""
    return Counter(case.cls for fp in MODES for cases in emulator_cases(arch, fp).values() for case in cases)


LOADS, STORES = ("LDG", "LDS", "LDL", "LDC", "ULDC", "LD"), ("STG", "STS", "STL", "ST")


def address(d):
    """A load's or store's address operands (the md names them Ra* or memoryDescriptor)."""
    return [n for n in d.klass.operand_types if n.startswith("Ra") or n == "memoryDescriptor"] if d.klass.mnemonic in LOADS + STORES else []


def backward(insts, branch=lambda o: False):
    """Offsets whose results reach an output store (STG), following each instruction's reads; a
    sliced load also needs every earlier store, through memory."""
    needed, memory, out = set(), False, set()
    for o, d, reads, writes in reversed(insts):
        if d.klass.mnemonic == "STG" or writes & needed or memory and d.klass.mnemonic in STORES or branch(o, d):
            out.add(o); needed = needed - writes | reads; memory |= d.klass.mnemonic in LOADS
    return out


def on_data_path(g, code):
    """Offsets of the instructions a fixture's output comparison checks, over md register reads and
    writes (tests/harness/dataflow.py:expected) in code order: those whose results reach the stored
    data other than through a load or store address (constants included), and those that see the
    test vectors (the forward slice of the input loads, LDG) and reach the output at all (an index
    into a table, a branch on a sliced predicate). A store of a sliced value carries it through
    memory to later loads."""
    insts = [(o, d, *expected(g, d, optimized=True)) for o, d in decoded("SM89", code)]
    tainted, memory, forward = set(), False, set()
    for o, d, reads, writes in insts:
        if d.klass.mnemonic == "LDG" or reads & tainted or memory and d.klass.mnemonic in LOADS:
            forward.add(o); tainted |= writes; memory |= d.klass.mnemonic in STORES
        else: tainted -= writes
    branch = lambda o, d: o in forward and g.props.get(d.klass.name, {}).get("BRANCH_TYPE", "BRT_NONE") != "BRT_NONE"
    data = [(o, d, expected(g, d, optimized=True, ignore=address(d))[0], writes) for o, d, _, writes in insts]
    return backward(data, branch) | forward & backward(insts, branch)


@cache
def gpu():
    """{class: [fixture names]} for the SM89 classes on the data path of a GPU fixture that passed
    on the GPU exactly as it is now (tests/baseline/gpu_passes.json, written by `pytest -m gpu`)."""
    g, out = Gen("SM89"), defaultdict(set)
    for f in (f for _, f in gpu_fixtures() if passed(f)):
        code = Template(f.image, f.kernel).code
        sliced = on_data_path(g, code)
        for o, d in decoded("SM89", code):
            if o in sliced: out[d.klass.name].add(f.name)
    return out


def ptxas(arch):
    """{class: [probe paths]} from the annotated probes targeting `arch`; fails on a broken expectation."""
    out, broken = defaultdict(set), []
    for probe in sorted(PROBES.rglob("*.ptx")):
        text, name = probe.read_text(), str(probe.relative_to(PROBES))
        want = [t for line in re.findall(r"^// sass: (.+)$", text, re.M) for t in line.split()]
        absent = [t for line in re.findall(r"^// sass-absent: (.+)$", text, re.M) for t in line.split()]
        target = re.search(r"^\.target (sm_(\d+)\w*)", text, re.M)
        if not want and not absent or f"SM{target[2]}" != arch: continue
        target = target[1]
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "probe.cubin"
            subprocess.run([PTXAS, f"-arch={target}", "-O3", str(probe), "-o", str(image)], check=True, capture_output=True)
            data = image.read_bytes()
        found = [(mnemonic(d), d.klass.name) for _, d in decoded(arch, b"".join(cubin.text_words(data)))]
        broken += [f"{name}: ptxas emitted no {t}" for t in want if not any(hits(op, t) for op, _ in found)]
        broken += [f"{name}: ptxas emitted {t}" for t in absent if any(hits(op, t) for op, _ in found)]
        for op, cls in found:
            if any(hits(op, t) for t in want): out[cls].add(name)
    if broken: raise SystemExit("\n".join(broken))
    return out


def evidence(arch):
    """{class: {"gpu": [...], "ptxas": [...], "reference": n}}, keys present only when non-empty."""
    sources = {"gpu": gpu() if arch == "SM89" else {}, "ptxas": ptxas(arch), "reference": reference(arch)}
    classes = sorted({cls for found in sources.values() for cls in found})
    return {cls: {kind: sorted(found[cls]) if isinstance(found[cls], set) else found[cls]
                  for kind, found in sources.items() if found.get(cls)} for cls in classes}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("architectures", nargs="*", default=ARCHES, metavar="SMxx")
    p.add_argument("--output", type=Path, default=LANGUAGES)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(min(len(a.architectures), os.cpu_count() or 1)) as pool:
        for arch, found in zip(a.architectures, pool.map(evidence, a.architectures)):
            (a.output / f"sass_{arch.lower()}_evidence.json").write_text(json.dumps(found, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
