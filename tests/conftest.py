"""Test layers (see AGENTS.md): 0 helpers, 1 pydecode vs nvdisasm, 2 SLEIGH disassembly, 3 p-code
dataflow, 4 p-code semantics, 5 references vs GPU. Markers express requirements: `ghidra`
(compiled languages and pyghidra) and `gpu` (an SM89 device). Plain `pytest tests` runs neither."""
import pytest
from tests.paths import ARCHES, FP_MODES, LANGUAGES


def pytest_addoption(parser):
    parser.addoption("--update-baseline", action="store_true", help="accept current results as the baseline")
    parser.addoption("--full", action="store_true", help="use the whole real corpus instead of a sample")
    parser.addoption("--arch", default="SM89", help="architecture(s) for Ghidra layers: SMxx[,SMyy] or all")
    parser.addoption("--fp", default="exact", help="fp lowering(s): exact, readable or all")
    parser.addoption("--family", action="append", help="limit semantics/GPU tests to these case families")
    parser.addoption("--require-gpu", action="store_true", help="fail instead of skipping without an SM89 device")


def pytest_configure(config):
    config.addinivalue_line("markers", "ghidra: needs compiled SLEIGH languages and Ghidra's pyghidra venv")
    config.addinivalue_line("markers", "gpu: needs an SM89 CUDA device")
    if not config.option.markexpr: config.option.markexpr = "not ghidra and not gpu"


def selected(config, marker):
    """Whether -m selects `marker` (expensive layers build their parameters only then)."""
    expr = config.option.markexpr
    return marker in expr and f"not {marker}" not in expr


def skipped(metafunc, name, marker):
    metafunc.parametrize(name, [pytest.param(None, marks=pytest.mark.skip(f"{marker} layer not selected (-m {marker})"))])


def option_list(config, name, universe):
    value = config.getoption(name)
    return list(universe) if value == "all" else value.split(",")


def arches(config): return option_list(config, "--arch", ARCHES)
def modes(config): return option_list(config, "--fp", FP_MODES)
def families(config):
    from tests.cases import FAMILIES
    return config.getoption("--family") or list(FAMILIES)


def require_languages(variants):
    """Fail early on a stale .sla: Ghidra recompiles it on load, and concurrent compilers corrupt it."""
    from sass.gen_sleigh import variant
    sources = [LANGUAGES / "sass_common.sinc"]
    for arch, fp in variants:
        sla, spec = LANGUAGES / f"sass_{variant(arch, fp)}.sla", LANGUAGES / f"sass_{variant(arch, fp)}.slaspec"
        if not sla.exists() or any(s.stat().st_mtime > sla.stat().st_mtime for s in [spec, *sources]):
            pytest.fail(f"{sla.name} is missing or stale: run `python3 -m sass.build_languages` first", pytrace=False)
