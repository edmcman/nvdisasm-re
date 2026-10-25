"""Coverage collection must not change generated code."""
import json
import pytest
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
    assert plain.coverage == {}
    plain.write_manifests(tmp_path)
    assert not list(tmp_path.glob("*.json"))
    detailed.write_manifests(tmp_path)
    assert json.loads((tmp_path / "sass_sm89_coverage.json").read_text())
