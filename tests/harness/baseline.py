"""Per-class pass/fail ratchets against tests/baseline/."""
import json
from pathlib import Path
import pytest

BASELINE = Path(__file__).parent.parent / "baseline"


def ratchet(path, now, update):
    """Fail on classes that regressed against the baseline at `path`; report newly passing ones."""
    if update: path.parent.mkdir(exist_ok=True); path.write_text(json.dumps(now, indent=0) + "\n")
    if not path.exists(): pytest.fail(f"{path.name} missing: run with --update-baseline", pytrace=False)
    base = json.loads(path.read_text())
    regressed = sorted(c for c, ok in now.items() if base.get(c) and not ok)
    improved = sorted(c for c, ok in now.items() if ok and base.get(c) is False)
    if improved: print(f"{path.stem}: newly passing (run --update-baseline): {improved}")
    assert not regressed, f"{path.stem}: classes regressed: {regressed}"
