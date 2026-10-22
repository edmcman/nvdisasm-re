"""The Ghidra-free semantics (sass.semantics.bitvec families, lift + execute) compute the independent
references' values on the emulator cases (the same as test_pcode_semantics). One item per (architecture, fp, family, md class)
whose opcode has a semantics family; no Ghidra needed."""
from functools import cache
from sass.decode import ir
from sass.semantics.family import family_for
from tests.cases import stream
from tests.harness.select import arches, families, modes
from tests.harness import lifted, sampling, sharded


@cache
def modelled(arch):
    first = {}
    for k in ir.load(arch).classes: first.setdefault(k.name, k)
    return {name for name, k in first.items() if family_for(k)}


def judged(job):
    """(item, case) for each case of the job's shard that the lifted semantics can judge."""
    arch, fp, family, *_ = job
    for case in stream(*job):
        if case.cls in modelled(arch) and lifted.applies(case): yield (arch, fp, family, case.cls), case


def evaluate(job):
    """{item: [cases, failures, reports]} over one shard, in a worker process."""
    out = {}
    for item, case in judged(job):
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
    found = results(*selection(metafunc.config))
    metafunc.config._lifted_cases = sum(counts[0] for counts in found.values())
    params = sorted(found)
    metafunc.parametrize("lifted_class", params, ids=["-".join(p) for p in params])


def test_lifted_semantics(lifted_class, request):
    total, failed, reports = results(*selection(request.config))[lifted_class]
    assert not failed, f"{failed}/{total} cases failed\n" + "\n".join(reports)
