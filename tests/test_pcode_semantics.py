"""Generated p-code, run in Ghidra's emulator, computes the independent references'
values (tests/cases/*, tests/refs/*). One item per (architecture, fp lowering, family, md class)."""
from collections import Counter
from functools import cache
import pytest
from tests.harness.select import arches, families, modes, require_languages, selected, skipped
from sass import ghidra_bridge
from tests.harness.checks import problems
from tests.harness import sampling, sharded
from tests.cases import planned, stream

WORKERS = 24   # one emulation JVM each (~0.7 GB on SM90)


def selection(config):
    return tuple((a, f) for a in arches(config) for f in modes(config)), tuple(families(config)), sampling.configured(config)


def tally(job):
    arch, fp, family, *_ = job
    return Counter((arch, fp, family, case.cls) for case in stream(*job))


def evaluate(job):
    """{item: [cases, failures, reports]} for one shard, emulated by one Ghidra process."""
    arch, fp, family, *_ = job
    cases = list(stream(*job))
    out = {}
    for case, result in zip(cases, ghidra_bridge.emulate(arch, fp, [c.request for c in cases])):
        found = problems(case.expected, result)
        report = [f"  {found}: request {case.request}\n    expected {case.expected}\n    got {result}"] if found else []
        sharded.merge(out, (arch, fp, family, case.cls), 1, int(bool(found)), report)
    return out


@cache
def items(variants, fams, sample):
    """The manual cases' classes and the saved plans' generated ones, without generating the latter."""
    generated = {(a, f, family, cls) for a, f in variants for family in fams for cls in planned(a)[family]}
    return sorted(generated.union(*sharded.run(tally, variants, fams, sample, shards=sharded.manual)))


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
    found = sharded.collect(sharded.run(evaluate, variants, fams, sample, WORKERS))
    request.config._ghidra_cases = sum(counts[0] for counts in found.values())
    return found


@pytest.mark.ghidra
def test_pcode_semantics(semantic_class, semantic_results):
    total, failed, reports = semantic_results[semantic_class]
    assert not failed, f"{failed}/{total} cases failed\n" + "\n".join(reports)
