"""Coverage collection must not change generated code or primitive calling conventions."""
import json
import pytest
from sass.semantics import runtime
from sass.sleigh import gen, modifiers


def instruction(coverage):
    g = gen.Gen("SM89", coverage=coverage)
    k = next(k for k in g.arch.classes if k.name == "prmt__RuIR_RIR")
    g.generate = lambda: [g.build(k, None, [], {})[0]]
    return g


def test_coverage_is_optional(tmp_path, monkeypatch):
    detailed = instruction(True)
    expected = detailed.slaspec()
    def forbidden(*args):
        pytest.fail("default generation enumerated coverage combinations")
    monkeypatch.setattr(modifiers, "combinations", forbidden)
    plain = instruction(False)
    assert plain.slaspec() == expected
    assert plain.runtime == detailed.runtime
    assert plain.coverage == {}
    plain.write_manifests(tmp_path)
    assert not list(tmp_path.glob("*_coverage.json"))
    assert json.loads((tmp_path / "sass_sm89_runtime.json").read_text())
    detailed.write_manifests(tmp_path)
    assert json.loads((tmp_path / "sass_sm89_coverage.json").read_text())


@pytest.mark.parametrize("legacy", [False, True])
def test_runtime_manifest(tmp_path, legacy):
    name = "coverage" if legacy else "runtime"
    rows = [{"cls": "example", "variants": [{"inputs": ["Ra"], "outputs": {"Rd": 4}}]}]
    (tmp_path / f"sass_sm89_{name}.json").write_text(json.dumps(rows))
    r = runtime.Runtime(tmp_path, 89)
    assert r.manifest(89) == {"example": rows[0]}
