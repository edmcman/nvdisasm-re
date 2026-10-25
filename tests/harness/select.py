"""Test selection from the pytest options (see conftest.py) and the stale-language guard."""
import pytest
from sass.paths import LANGUAGES
from sass.sleigh.build import ARCHES
from sass.sleigh.gen import MODES


def selected(config, marker=None):
    """Whether -m names `marker` (expensive tests build their parameters only then), or, when None,
    whether it selects unmarked tests."""
    expr = config.option.markexpr
    if marker: return marker in expr and f"not {marker}" not in expr
    from _pytest.mark.expression import Expression
    return not expr or Expression.compile(expr).evaluate(lambda name, **kwargs: False)


def skipped(metafunc, name, marker, reason=None):
    metafunc.parametrize(name, [pytest.param(None, marks=pytest.mark.skip(reason or f"{marker} tests not selected (-m {marker})"))])


def option_list(config, name, universe):
    value = config.getoption(name)
    if name == "--arch" and value is None: value = "SM89"
    return list(universe) if value == "all" else value.split(",")


def arches(config): return option_list(config, "--arch", ARCHES)
def modes(config): return option_list(config, "--fp", MODES)
def families(config):
    from tests.cases import FAMILIES
    return config.getoption("--family") or list(FAMILIES)


def require_languages(variants):
    """Fail early on a stale .sla: Ghidra recompiles it on load, and concurrent compilers corrupt it."""
    from sass.paths import variant
    sources = [LANGUAGES / "sass_common.sinc"]
    for arch, fp in variants:
        sla, spec = LANGUAGES / f"sass_{variant(arch, fp)}.sla", LANGUAGES / f"sass_{variant(arch, fp)}.slaspec"
        if not sla.exists() or any(s.stat().st_mtime > sla.stat().st_mtime for s in [spec, *sources]):
            pytest.fail(f"{sla.name} is missing or stale: run `python3 -m sass.sleigh.build` first", pytrace=False)
