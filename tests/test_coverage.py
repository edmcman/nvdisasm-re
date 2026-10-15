"""The committed semantic coverage summary matches the generated languages' manifests."""
import json
import pytest
from sass.sleigh import coverage
from sass.paths import LANGUAGES, ROOT

SUMMARY = ROOT / "processor/SASS/semantics/coverage.json"


@pytest.mark.ghidra
def test_coverage(request):
    now = coverage.summarize(LANGUAGES)
    if request.config.getoption("--update-baseline"): coverage.write(now, SUMMARY)
    committed = json.loads(SUMMARY.read_text())
    differ = {arch: [k for k in now[arch] if now[arch][k] != committed.get(arch, {}).get(k)] for arch in now if now[arch] != committed.get(arch)}
    assert not differ and set(now) == set(committed), f"coverage.json is stale (python -m sass.sleigh.coverage {LANGUAGES} --output {SUMMARY}): {differ}"
