"""Generated p-code, run in Ghidra's emulator, computes the independent references'
values (tests/cases/*, tests/refs/*). One item per (architecture, fp lowering, family, md class)."""
import hashlib, json
from collections import Counter
from functools import cache
import pytest
from tests.harness.select import arches, families, modes, require_languages, selected, skipped
from sass import ghidra_bridge
from sass.paths import CACHE, ROOT
from tests.harness.checks import problems
from tests.harness import sampling, sharded
from tests.cases import emulator_cases, planned, stream, ghidra_case


def selection(config):
    return tuple((a, f) for a in arches(config) for f in modes(config)), tuple(families(config)), sampling.configured(config)


def tally(job):
    """The classes of a manual shard's selected cases (sharded.manual), left incomplete: only their classes are read."""
    arch, fp, family, sample, _ = job
    return Counter((arch, fp, family, case.cls) for case in emulator_cases(arch, fp, (family,), sample, generated=False, complete=False)[family])


def evaluate(job):
    """{item: [cases, failures, reports]} for one shard, emulated by one Ghidra process."""
    arch, fp, family, *_ = job
    cases = [adapted for c in stream(*job) if (adapted := ghidra_case(arch, c)) is not None]
    out = {}
    for case, result in zip(cases, ghidra_bridge.emulate(arch, fp, [c.request for c in cases])):
        found = problems(case.expected, result)
        report = [f"  {found}: request {case.request}\n    expected {case.expected}\n    got {result}"] if found else []
        sharded.merge(out, (arch, fp, family, case.cls), 1, int(bool(found)), report)
    return out


def source_digest(arches):
    """What the manual cases are built from: the sass and test sources and the languages' md."""
    h = hashlib.sha256()
    for root in (ROOT / "src/sass", ROOT / "tests", *(ROOT / "out" / a for a in arches)):
        for p in sorted(root.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts: h.update(str(p.relative_to(ROOT)).encode()); h.update(p.read_bytes())
    return h.hexdigest()[:16]


def manual_items(variants, fams, sample):
    """The manual cases' items, kept on disk per source digest: building every manual case only to
    name the items took minutes per run, and evaluation builds them again."""
    selection = hashlib.sha256(repr((variants, fams, sample)).encode()).hexdigest()[:16]
    path = CACHE / "items" / f"{selection}-{source_digest(dict.fromkeys(a for a, _ in variants))}.json"
    if path.exists(): return {tuple(item) for item in json.loads(path.read_text())}
    found = set().union(*sharded.run(tally, variants, fams, sample, shards=sharded.manual))
    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in path.parent.glob(f"{selection}-*.json"): stale.unlink(missing_ok=True)
    temporary = path.with_suffix(".tmp"); temporary.write_text(json.dumps(sorted(found))); temporary.replace(path)
    return found


@cache
def items(variants, fams, sample):
    """The manual cases' classes and the saved plans' generated ones, without generating the latter."""
    generated = {(a, f, family, cls) for a, f in variants for family in fams for cls in planned(a)[family]}
    return sorted(generated | manual_items(variants, fams, sample))


def pytest_generate_tests(metafunc):
    if "semantic_class" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "ghidra"): return skipped(metafunc, "semantic_class", "ghidra")
    params = items(*selection(metafunc.config))
    metafunc.parametrize("semantic_class", params, ids=["-".join(p) for p in params])


@pytest.fixture(scope="session")
def semantic_results(request):
    """{item: [cases, failures, reports]}, every language emulated shard by shard."""
    variants, fams, sample = selection(request.config)
    require_languages(variants)
    found = sharded.collect(sharded.run(evaluate, variants, fams, sample, ghidra_bridge.jvm_workers()))
    request.config._ghidra_cases = sum(counts[0] for counts in found.values())
    return found


@pytest.mark.ghidra
def test_pcode_semantics(semantic_class, semantic_results):
    total, failed, reports = semantic_results[semantic_class]
    assert not failed, f"{failed}/{total} cases failed\n" + "\n".join(reports)
