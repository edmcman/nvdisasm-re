"""Deterministic input sampling with a per-modifier-form coverage floor.

Selection is local to a manual family or a generated planned form. No global
case list is needed, and generated shard boundaries cannot change the sample.
"""
import argparse
import hashlib
from functools import lru_cache
from sass.decode import pydecode
from sass.sleigh.modifiers import modifiers, name, settings

VERSION = 3
DEFAULT = 0
COMPLETE = 100


def percentage(value):
    """Parse an integer percentage; reject bools rather than silently treating them as 0/1."""
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        raise ValueError('sampling percentage must be an integer from 0 to 100')
    value = int(value)
    if not 0 <= value <= 100:
        raise ValueError('sampling percentage must be an integer from 0 to 100')
    return value


def argument(value):
    try: return percentage(value)
    except ValueError as error: raise argparse.ArgumentTypeError(str(error)) from error


def add_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--sample', type=argument, default=None, metavar='PERCENT',
                       help='shared-case sampling: 0 keeps form coverage and regressions (default), 100 runs all cases')
    group.add_argument('--exhaustive', action='store_true', help='alias for --sample 100')


def resolve(sample=None, exhaustive=False):
    if sample is not None and exhaustive:
        raise ValueError('--sample and --exhaustive are mutually exclusive')
    return COMPLETE if exhaustive else DEFAULT if sample is None else percentage(sample)


def configured(config):
    return resolve(config.getoption('--sample'), config.getoption('--exhaustive'))


@lru_cache(maxsize=None)   # a language's distinct words, which a 4096 bound thrashed on (SM107 F2FP keeps 10,542)
def form(arch, word):
    d = pydecode.decode(arch, bytes.fromhex(word))
    return d.klass.name, tuple(sorted(settings(arch, d).items()))


DECLARED = {}


@lru_cache(maxsize=None)
def modifier_names(arch, cls):
    from sass.assemble import encoding
    return frozenset(name(key) for key, _ in modifiers(encoding.klass(arch, cls)))


def declared(arch, cls, fields):
    """The form of an unencoded case: a word's form depends only on its class and modifier fields,
    so each combination is encoded once (Cases.complete checks every case it encodes)."""
    from tests.harness.case import encode
    key = arch, cls, tuple(sorted((n, v) for n, v in fields.items() if n in modifier_names(arch, cls)))
    if key not in DECLARED: DECLARED[key] = form(arch, encode(arch, cls, **fields))
    return DECLARED[key]


def rank(case, identity=None):
    # Generated descriptions use fields/state without encoding; manual cases use
    # scenario identity. Neither includes expected values or p-code inspection.
    return int.from_bytes(hashlib.sha256(f'sample-v{VERSION}:{case.id if identity is None else identity}'.encode()).digest())


def case_form(arch, case):
    """(class, modifier settings) of a case, encoded or not."""
    word = case.request.get('word')
    return getattr(case, 'modifier_form', None) or (form(arch, word) if word else declared(arch, case.cls, case.fields))


def buckets(arch, case):
    """One ordinary floor plus any applicable backend floors for this decoded form."""
    key, word = case_form(arch, case), case.request.get('word')
    yield key, 'reference'
    from tests.harness import lifted
    if lifted.applies(case): yield key, 'lifted'
    if arch in ('SM89', 'SM90'):
        from tests.harness import device_cases
        from sass.assemble import encoding
        klass = None if word else encoding.klass(arch, case.cls)
        if device_cases.classify(case, klass=klass, arch=arch)[0] != 'emulator': yield key, 'gpu'


def select(cases, arch, sample=DEFAULT, retained=()):
    """Sample eligible cases, always retaining regressions and a case per available form.

    Reference filtering and final hardware annotations must precede this call.
    Generated inputs can be unencoded descriptions with a declared modifier_form.
    Keep separate assertions even if they share a scenario ID. For a fixed corpus,
    increasing sample only adds cases; 100 preserves the original list verbatim.
    """
    sample = percentage(sample)
    if sample == COMPLETE: return cases
    threshold = (1 << 256) * sample // 100
    retained = set(retained)
    chosen, anchors = set(), {}
    identities = getattr(cases, "sampling_ids", {})
    for i, case in enumerate(cases):
        case_id = case.id
        if case_id in retained or 'word' not in case.request or 'error' in case.expected or '__opaque' in case.expected:
            chosen.add(i)
            continue
        identity = identities.get(case_id, case_id)
        r = rank(case, identity)
        if r < threshold: chosen.add(i)
        for key in buckets(arch, case):
            # IDs break digest ties without depending on generation order.
            candidate = (r, identity)
            if key not in anchors or candidate < anchors[key][0]: anchors[key] = candidate, [i]
            elif candidate == anchors[key][0]: anchors[key][1].append(i)
    chosen.update(i for _, indices in anchors.values() for i in indices)
    return [case for i, case in enumerate(cases) if i in chosen]
