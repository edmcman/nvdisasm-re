"""The Ghidra-free semantics (sass.semantics.bitvec families, lift + execute) compute the independent
references' values on the emulator cases (the same as test_pcode_semantics). One item per (architecture, fp, family, md class)
whose opcode has a semantics family; no Ghidra needed."""
from concurrent.futures import ProcessPoolExecutor
from functools import cache
from sass.decode import ir
from sass.semantics.family import family_for
from tests.cases import built_cases
from tests.harness.select import arches, families, modes
from tests.harness import lifted


@cache
def selected_cases(variants, fams, exhaustive):
    """{(arch, fp, family, cls): [case]}: the emulator cases the lifted semantics can judge."""
    out = {}
    for (arch, fp), built in built_cases(variants, fams, exhaustive).items():
        first = {}
        for k in ir.load(arch).classes: first.setdefault(k.name, k)
        modelled = {name for name, k in first.items() if family_for(k)}
        for family, cases in built.items():
            for case in cases:
                if case.cls in modelled and lifted.applies(case): out.setdefault((arch, fp, family, case.cls), []).append(case)
    return out


def evaluate(job):
    """(item, [(case index, problems)]) for one item's cases, in a worker process."""
    (arch, fp, *_), cases = job
    return job[0], [(j, found) for j, case in enumerate(cases) if (found := lifted.run(arch, fp, case)[1])]


@cache
def results(variants, fams, exhaustive):
    """Every item's failures, computed once across all cores (lift + execute is pure Python)."""
    with ProcessPoolExecutor() as pool:
        return dict(pool.map(evaluate, selected_cases(variants, fams, exhaustive).items(), chunksize=2))


def selection(config):
    return tuple((a, f) for a in arches(config) for f in modes(config)), tuple(families(config)), config.getoption("--exhaustive")


def pytest_generate_tests(metafunc):
    if "lifted_class" not in metafunc.fixturenames: return
    params = sorted(selected_cases(*selection(metafunc.config)))
    metafunc.parametrize("lifted_class", params, ids=["-".join(p) for p in params])


def test_lifted_semantics(lifted_class, request):
    arch, fp, family, cls = lifted_class
    cases = selected_cases(*selection(request.config))[lifted_class]
    failed = [(cases[j], found) for j, found in results(*selection(request.config))[lifted_class]]
    assert not failed, f"{len(failed)}/{len(cases)} cases failed\n" + "\n".join(
        f"  {found}: request {case.request}\n    expected {case.expected}" for case, found in failed[:5])
