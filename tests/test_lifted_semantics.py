"""The Ghidra-free semantics (sass.semantics.bitvec families, lift + execute) compute the independent
references' values on the emulator cases (the same as test_pcode_semantics). One item per (architecture, fp, family, md class)
whose opcode has a semantics family; no Ghidra needed."""
from functools import cache
from tests.harness.select import arches, families, modes, selected, skipped
from tests.harness import lifted, sampling, sharded


def evaluate(job):
    """{item: [cases, failures, reports]} over one shard, in a worker process."""
    out = {}
    for item, case in lifted.judged(job):
        found = lifted.run(item[0], item[1], case)[1]
        sharded.merge(out, item, 1, int(bool(found)), [f"  {found}: request {case.request}\n    expected {case.expected}"] if found else [])
    return out


@cache
def results(variants, fams, sample):
    """{item: [cases, failures, reports]} (lift + execute is pure Python), evaluated when the items
    are collected: which cases apply is known only once they are generated, and generating them
    twice cost as much again."""
    return sharded.collect(sharded.run(evaluate, variants, fams, sample))


def selection(config):
    return tuple((a, f) for a in arches(config) for f in modes(config)), tuple(families(config)), sampling.configured(config)


def pytest_generate_tests(metafunc):
    if "lifted_class" not in metafunc.fixturenames: return
    if not selected(metafunc.config): return skipped(metafunc, "lifted_class", "unmarked", "unmarked tests not selected by -m")
    found = results(*selection(metafunc.config))
    metafunc.config._lifted_cases = sum(counts[0] for counts in found.values())
    params = sorted(found)
    metafunc.parametrize("lifted_class", params, ids=["-".join(p) for p in params])


def test_lifted_semantics(lifted_class, request):
    total, failed, reports = results(*selection(request.config))[lifted_class]
    assert not failed, f"{failed}/{total} cases failed\n" + "\n".join(reports)
