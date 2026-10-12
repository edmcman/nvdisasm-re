"""Layer 4a: the Ghidra-free semantics (sass.sem families, lift + execute) compute the independent
references' values on the layer-4 emulator cases. One item per (architecture, fp, family, md class)
whose opcode has a semantics family; no Ghidra needed."""
from concurrent.futures import ProcessPoolExecutor
from functools import cache
from sass import ir
from tests.conftest import arches, families, modes
from tests.harness import lifted


def build(job):
    """{(arch, fp, family, cls): [case]} for one language (cases are built in worker processes)."""
    from tests.cases import emulator_cases
    arch, fp, fams = job
    md, out = ir.load(arch), {}
    for family, cases in emulator_cases(arch, fp, fams).items():
        for case in cases:
            if lifted.applies(case) and lifted.has_family(md, case.cls): out.setdefault((arch, fp, family, case.cls), []).append(case)
    return out


@cache
def selected_cases(variants, fams):
    with ProcessPoolExecutor() as pool:
        return {k: v for part in pool.map(build, [(a, f, fams) for a, f in variants]) for k, v in part.items()}


def selection(config):
    return tuple((a, f) for a in arches(config) for f in modes(config)), tuple(families(config))


def pytest_generate_tests(metafunc):
    if "lifted_class" not in metafunc.fixturenames: return
    params = sorted(selected_cases(*selection(metafunc.config)))
    metafunc.parametrize("lifted_class", params, ids=["-".join(p) for p in params])


def test_sem_semantics(lifted_class, request):
    arch, fp, family, cls = lifted_class
    cases = selected_cases(*selection(request.config))[lifted_class]
    failed = [(case, found) for case in cases if (found := lifted.run(arch, fp, case)[1])]
    assert not failed, f"{len(failed)}/{len(cases)} cases failed\n" + "\n".join(
        f"  {found}: request {case.request}\n    expected {case.expected}" for case, found in failed[:5])
