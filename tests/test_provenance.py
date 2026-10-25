"""A word's evidence is the kinds that ran its class and exact form: reference and ptxas on its own
architecture, gpu on any architecture with GPU evidence for the class."""
from sass import provenance
from sass.assemble import encoding
from sass.sleigh.modifiers import compact

BMSK, MOV = 'bmsk__RCR_RCR', 'mov__RR'
RECORDED = {
    'SM80': {BMSK: {'modifiers': ['cw'], 'forms': {'reference': [['W']]}}},
    'SM89': {BMSK: {'modifiers': ['cw'], 'forms': {'gpu': [['C']], 'reference': [['C'], ['W']]}},
             MOV: {'modifiers': [], 'forms': {'gpu': [[]]}}},
    'SM90': {BMSK: {'modifiers': ['cw'], 'forms': {'gpu': [['C'], ['W']], 'ptxas': [['W']]}}},
}
RECORDED = {a: {c: compact(e) for c, e in classes.items()} for a, classes in RECORDED.items()}   # as evidence.json commits them


def word(arch, cls, **values):
    return bytes.fromhex(encoding.encode(arch, cls, **values))


def test_kinds_and_their_architectures(monkeypatch):
    monkeypatch.setattr(provenance, 'recorded', lambda: RECORDED)
    monkeypatch.setattr(provenance, 'gpu_architectures', lambda: ('SM89', 'SM90'))
    assert provenance.evidence('SM89', word('SM89', BMSK, cw='C', Sb_bank=0, Sb_addr=0)) == {'gpu': {'SM89', 'SM90'}, 'reference': {'SM89'}}
    assert provenance.evidence('SM89', word('SM89', BMSK, cw='W', Sb_bank=0, Sb_addr=0)) == {'gpu': {'SM90'}, 'reference': {'SM89'}}
    assert provenance.evidence('SM90', word('SM90', BMSK, cw='W', Sb_bank=0, Sb_addr=0)) == {'gpu': {'SM90'}, 'ptxas': {'SM90'}}
    assert provenance.evidence('SM80', word('SM80', BMSK, cw='W', Sb_bank=0, Sb_addr=0)) == {'gpu': {'SM90'}, 'reference': {'SM80'}}
    assert provenance.evidence('SM89', word('SM89', MOV)) == {'gpu': {'SM89'}}
    assert provenance.evidence('SM90', word('SM90', MOV)) == {'gpu': {'SM89'}}
    monkeypatch.setattr(provenance, 'gpu_architectures', lambda: ('SM90',))
    assert provenance.evidence('SM89', word('SM89', MOV)) == {}


def test_committed_gpu_architectures():
    assert {'SM89', 'SM90'} <= set(provenance.gpu_architectures())
