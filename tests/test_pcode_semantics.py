"""Generated p-code, run in Ghidra's emulator, computes the independent references'
values (tests/cases/*, tests/refs/*). One item per (architecture, fp lowering, family, md class)."""
from collections import defaultdict
import pytest
from tests.harness.select import arches, families, modes, require_languages, selected, skipped
from sass import ghidra_bridge
from tests.harness.checks import problems
from tests.cases import built_cases


def selection(config):
    return tuple((a, f) for a in arches(config) for f in modes(config)), tuple(families(config)), config.getoption("--exhaustive")


def pytest_generate_tests(metafunc):
    if "semantic_class" not in metafunc.fixturenames: return
    if not selected(metafunc.config, "ghidra"): return skipped(metafunc, "semantic_class", "ghidra")
    cases = built_cases(*selection(metafunc.config))
    params = [(arch, fp, family, cls) for (arch, fp), fams in cases.items() for family, cs in fams.items() for cls in dict.fromkeys(c.cls for c in cs)]
    metafunc.parametrize("semantic_class", params, ids=["-".join(p) for p in params])


@pytest.fixture(scope="session")
def semantic_results(request):
    """{(arch, fp): {(family, cls): [(case, result)]}}, every language emulated concurrently."""
    variants, fams, exhaustive = selection(request.config)
    require_languages(variants)
    cases = built_cases(variants, fams, exhaustive)
    flat = {v: [(family, c) for family, cs in fams_cases.items() for c in cs] for v, fams_cases in cases.items()}
    results = ghidra_bridge.emulate_sharded({v: (*v, [c.request for _, c in flat[v]]) for v in variants})
    out = {}
    for v in variants:
        grouped = defaultdict(list)
        for (family, case), result in zip(flat[v], results[v]): grouped[family, case.cls].append((case, result))
        out[v] = grouped
    return out


@pytest.mark.ghidra
def test_pcode_semantics(semantic_class, semantic_results):
    arch, fp, family, cls = semantic_class
    failed = [(case, result, found) for case, result in semantic_results[arch, fp][family, cls] if (found := problems(case.expected, result))]
    total = len(semantic_results[arch, fp][family, cls])
    assert not failed, f"{len(failed)}/{total} cases failed\n" + "\n".join(
        f"  {found}: request {case.request}\n    expected {case.expected}\n    got {result}" for case, result, found in failed[:5])
