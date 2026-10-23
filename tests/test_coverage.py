"""The committed semantic coverage summary matches the generated languages' manifests."""
import json
import pytest
from sass.sleigh import coverage
from sass.paths import LANGUAGES, ROOT

SUMMARY = ROOT / "processor/SASS/semantics/coverage.json"


@pytest.mark.ghidra
def test_coverage(request):
    now = coverage.summarize(LANGUAGES)
    if request.config.getoption("--update-baseline"): coverage.write(LANGUAGES, SUMMARY)
    committed = json.loads(SUMMARY.read_text())
    differ = {arch: [k for k in now[arch] if now[arch][k] != committed.get(arch, {}).get(k)] for arch in now if now[arch] != committed.get(arch)}
    assert not differ and set(now) == set(committed), f"coverage.json is stale ({coverage.REFRESH}): {differ}"


def test_committed_evidence_forms_round_trip():
    e = dict(gpu=['cases/fp32'], reference=3, modifiers=['ftz', 'rnd'],
             forms=dict(gpu=[['FTZ', 'RN']], ptxas=[[None, 'RZ']], reference=[['FTZ', 'RN'], ['noftz', 'RZ']]))
    packed = coverage.compact(e)
    assert packed['labels'] == [['FTZ', 'noftz', None], ['RN', 'RZ']]
    assert packed['forms'] == {'gpu+reference': ['00'], 'ptxas': ['21'], 'reference': ['11']}
    assert coverage.expand(packed) == e
    assert coverage.compact({'reference': 1}) == coverage.expand({'reference': 1}) == {'reference': 1}
