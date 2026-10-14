"""Generated p-code reads and writes exactly the md operand roles and spans, and its
flow type matches BRANCH_TYPE; per-class p-code op counts are ratcheted in baseline/pcode_*.json."""
import json
from collections import Counter
import pytest
from sass import synth
from tests.cases import f2fp
from sass.check.pydecode_disasm import sample
from tests.harness.select import arches, modes, require_languages, selected, skipped
from tests.harness import dataflow
from sass import ghidra_bridge
from tests.harness.baseline import BASELINE
from sass.sleigh.gen import variant

REAL_SAMPLE = 2000


def words(arch, full):
    """Synthetic words (8 per class), sampled real words, and the focused F2FP forms."""
    return list(synth.synthetic(arch, 8)) + sample(arch, "real", 0 if full else REAL_SAMPLE)[0] + f2fp.dataflow_words(arch)


def dataflow_problems(arch, fp, full): return dataflow.problems(arch, fp, words(arch, full))


def pytest_generate_tests(metafunc):
    if "language" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "ghidra"): return skipped(metafunc, "language", "ghidra")
    params = [(a, f) for a in arches(metafunc.config) for f in modes(metafunc.config)]
    metafunc.parametrize("language", params, ids=["-".join(p) for p in params])


@pytest.fixture(scope="session")
def dataflow_results(request):
    variants = [(a, f) for a in arches(request.config) for f in modes(request.config)]
    require_languages(variants)
    full = request.config.getoption("--full")
    return ghidra_bridge.concurrently(dataflow_problems, {v: (*v, full) for v in variants})


@pytest.mark.ghidra
def test_pcode_dataflow(language, dataflow_results):
    total, failed = dataflow_results[language]
    assert not failed, f"{len(failed)}/{total} words\n" + "\n".join(failed[:20])


def op_counts(arch, fp):
    """{class: {p-code op: count}} for one md-legal synthetic word per class."""
    labels = synth.synthetic(arch, 1, 0, legal=True)
    results = ghidra_bridge.disassemble(arch, fp, [(w, 0) for w in labels], "--dataflow")
    return {labels[w].removeprefix("synth:"): dict(sorted(Counter(op["op"] for op in r["ops"]).items())) if "ops" in r else {"error": 1}
            for w, r in zip(labels, results)}


@pytest.fixture(scope="session")
def pcode_ops(request):
    variants = [(a, f) for a in arches(request.config) for f in modes(request.config)]
    require_languages(variants)
    return ghidra_bridge.concurrently(op_counts, {v: v for v in variants})


@pytest.mark.ghidra
def test_pcode_ops(language, pcode_ops, request):
    """Generated p-code shape changes only deliberately (--update-baseline after reviewing)."""
    path, now = BASELINE / f"pcode_{variant(*language)}.json", pcode_ops[language]
    if request.config.getoption("--update-baseline"): path.write_text(json.dumps(now, indent=0, sort_keys=True) + "\n")
    if not path.exists(): pytest.fail(f"{path.name} missing: run with --update-baseline", pytrace=False)
    base = json.loads(path.read_text())
    changed = sorted(c for c in set(base) | set(now) if base.get(c) != now.get(c))
    assert not changed, f"{len(changed)} classes changed p-code: " + ", ".join(f"{c}: {base.get(c)} -> {now.get(c)}" for c in changed[:10])
