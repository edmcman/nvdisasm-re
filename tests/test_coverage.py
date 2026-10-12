"""The committed semantic coverage summary matches the generated languages' manifests."""
import json
import pytest
from sass import coverage
from tests.paths import LANGUAGES, ROOT

SUMMARY = ROOT / "processor/SASS/semantics/coverage.json"


@pytest.mark.ghidra
def test_coverage(request):
    now = coverage.summarize(LANGUAGES)
    if request.config.getoption("--update-baseline"): SUMMARY.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
    committed = json.loads(SUMMARY.read_text())
    differ = {arch: [k for k in now[arch] if now[arch][k] != committed.get(arch, {}).get(k)] for arch in now if now[arch] != committed.get(arch)}
    assert not differ and set(now) == set(committed), f"coverage.json is stale (python -m sass.coverage {LANGUAGES} --output {SUMMARY}): {differ}"
