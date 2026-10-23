"""Which evidence ran an instruction word's class and form, from the committed evidence.json
(tests/evidence.py): `reference` and `ptxas` from the word's own architecture, `gpu` from every
architecture with GPU evidence for the same class, each kind with the architectures behind it."""
import json
from functools import cache
from sass.decode import pydecode
from sass.paths import EVIDENCE
from sass.sleigh.modifiers import settings


@cache
def recorded():
    if not EVIDENCE.exists():
        raise FileNotFoundError(f"{EVIDENCE}: set $SASS_EVIDENCE to a copy of processor/SASS/semantics/evidence.json")
    return json.loads(EVIDENCE.read_text())


@cache
def gpu_architectures():
    return tuple(a for a, classes in recorded().items() if any("gpu" in e for e in classes.values()))


def ran(entry, kind, s):
    """Whether `kind` ran the form with settings `s`, as evidence.json stores it: labels in the order of the entry's modifiers."""
    names = entry.get("modifiers", [])
    return set(s) <= set(names) and [s.get(m) for m in names] in entry.get("forms", {}).get(kind, [])


def evidence(arch, word):
    """{kind: frozenset(architectures)} of the evidence that ran the class and form of `word` on `arch`."""
    d = pydecode.decode(arch, word)
    s, cls = settings(arch, d), d.klass.name
    sources = {"reference": (arch,), "ptxas": (arch,), "gpu": gpu_architectures()}
    found = {k: frozenset(a for a in archs if ran(recorded().get(a, {}).get(cls, {}), k, s)) for k, archs in sources.items()}
    return {k: v for k, v in found.items() if v}
