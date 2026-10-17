"""MUFU outputs measured on the SM89 GPU (research/mufu/survey.py; hardware observations, not a model).

`samples()` gives each op's raw MUFU.<op> R11, R2 result for a fixed set of inputs (specials, powers of two and their
neighbours, values around 1, large arguments, random patterns); `survey()` the per-op histogram of the distance in ulps
to the correctly rounded result over every 32-bit input (ulps are measured on the high word for RCP64H/RSQ64H)."""
import json
from functools import cache
from pathlib import Path


@cache
def samples():
    """(inputs, {op: outputs}) as lists of 32-bit words."""
    data = json.loads((Path(__file__).parent / "mufu_samples.json").read_text())
    words = lambda text: [int(text[i:i + 8], 16) for i in range(0, len(text), 8)]
    return words(data["inputs"]), {op: words(text) for op, text in data["outputs"].items()}


@cache
def survey():
    return json.loads((Path(__file__).parent / "mufu_survey.json").read_text())
