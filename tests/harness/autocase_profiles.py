"""Declarative input corpora shared by every architecture's generated form plan.

The seeds and edge lists preserve the ordinary cases formerly in int_alu/int_simd.
Reference calculations and operand placement belong to autocases, not these profiles.
"""
import random
from functools import cache

MASK32 = 0xffffffff
VECTORS = ((0, 0, 0, 0, 0), (0, 0, 1, 0, 1), (0, 0, MASK32, 1, 0), (0, 0, 0x80000000, 1, 1),
           (0x80000000, 0x7fffffff, MASK32, 0, 1), (0x7fffffff, 0x80000000, 1, 1, 0),
           (MASK32, 0, 0x12345678, 1, 1), (0x807f01ff, 0x7f80ff00, 0x10203040, 0, 0))

PROFILES = {
    'bits': dict(values=(0, 1, 0x80000000, MASK32, 0x01234567, 0xaaaaaaaa, 0x55555555, 0x80000001, 0x80ff017f),
                 random=('brev_popc', 20), banks=((2, 16), (3, 24))),
    'sgxt': dict(values=(0, 1, 0x7f, 0x80, 0xff, 0x7fffffff, 0x80000000, MASK32, 0x80ff017f, 0x55555555),
                 widths=(0, 1, 7, 8, 31, 32, 33, 0xff, MASK32)),
    'permutation': dict(data=((0x80ff017f, 0x55aa0081), (0x03020100, 0x07060504)),
                        idx=tuple(i * 0x1111 for i in range(16)) + (0x0123, 0x6420, 0x89ab, 0xffff7654, 0x3210, 0x7654, 0xcdef, 0x0f1e),
                        other=tuple(range(16)) + tuple(i * 0x1111 for i in range(16)) + (0x0123, 0x6420, 0x89ab, 0xffff7654),
                        destinations=('separate', 'alias_left')),
    'integer_edges': dict(vectors=VECTORS),
    'comparison_legacy': dict(vectors=tuple((a, b, 0, p, low) for a, b, low in
        ((0, 0, 0), (0x80000000, 0x7fffffff, 1), (0x7fffffff, 0x80000000, 0), (MASK32, 0, 1)) for p in (0, 1))),
    'comparison_domain': dict(vectors=((0x81234567, 0x76543210, 0, 0, 0), (0x81234567, 0x76543210, 0, 1, 0))),
    'shift_edges': dict(vectors=tuple((a, shift, c, p, low) for (a, _, c, p, low), shift in
        zip(VECTORS, (0, 1, 31, 32, 33, 63, 64, 65)))),
    'shift_legacy': dict(vectors=tuple((0x81234567, shift, 0xa5a5f0f0, 0, 0) for shift in (0, 31, 32, 63, 64, 65)) +
                        ((0xabcdef12, 36, 0x12345678, 0, 0),)),
    'select_random': dict(random=('int_alu', 20), predicate='alternate'),
    'minmax_random': dict(random=('int_alu', 20), predicate='true'),
    'uniform_random': dict(random=('uniform', 12), predicate='alternate'),
    'uniform_compare': dict(random=('uniform', 12), predicate='true'),
    'carry_select': dict(words=((0, 0, 0), (MASK32, MASK32, MASK32), (0x80000000, 0x80000000, 0),
                               (MASK32, 1, 0), (0, MASK32, 1)), random=('carry', 12), predicate='both'),
    'dot4': dict(words=(0, MASK32, 0x807f01ff, 0x01020304, 0x7f80ff00), random=('idp4a', 8), offsets=(3, 5)),
    'dot2': dict(words=(0, MASK32, 0x80017fff, 0x7fff8001, 0x01020304), random=('idp2a', 8), offsets=(3, 5)),
}

# Supplements preserve seeded/manual input strategies on representative forms without
# multiplying random sweeps across the entire modifier product.
SUPPLEMENTS = (
    ('sel__RRR_RRR', {'Pp@not':'unset'}, 'select_random'),
    ('imnmx__RRR_RRR', {'fmt':'U32', 'Pp@not':'unset'}, 'minmax_random'),
    ('usel__URURUR_UUU', {}, 'carry_select'),
    ('usel__URURUR_UUU', {'UPp@not':'unset'}, 'uniform_random'),
    ('isetp__RUR_RUR_noEX', {'fmt':'U32', 'icmp':'LT', 'bop':'AND', 'Pp@not':'unset'}, 'uniform_compare'),
    ('uisetp__URURUR_URURUR', {'fmt':'U32', 'icmp':'LT', 'bop':'AND', 'UPp@not':'unset'}, 'uniform_compare'),
    ('ushf__URURUR_URURUR', {'fmt':'U32', 'dir':'R', 'cw':'W', 'hilo':'LO'}, 'uniform_random'),
    ('shf__RRR_RRR', {}, 'shift_legacy'),
    ('isetp__RRR_RRR_noEX', {}, 'comparison_domain'),
)


def names(op, cls, form):
    if op in ('BREV', 'POPC', 'FLO'): out = ['bits']
    elif op == 'SGXT': out = ['sgxt']
    elif op in ('PRMT', 'UPRMT'): out = ['permutation']
    elif op in ('SHF', 'USHF'): out = ['shift_edges']
    else:
        out = ['integer_edges']
        if op in ('ISETP', 'UISETP'): out.append('comparison_legacy')
        if op == 'IDP': out.append('dot4' if form.get('mode') == '4A' else 'dot2')
    return out + [profile for target, settings, profile in SUPPLEMENTS
                  if cls == target and all(form.get(k) == v for k, v in settings.items())]


@cache
def values(name):
    spec = PROFILES[name]
    out = list(spec.get('values', spec.get('words', ())))
    if 'random' in spec:
        seed, count = spec['random']; rng = random.Random(seed)
        out += [rng.getrandbits(32) for _ in range(count)]
    return tuple(out)


@cache
def vectors(name):
    spec = PROFILES[name]
    if 'vectors' in spec: return spec['vectors']
    if 'offsets' in spec:
        words = values(name); b, c = spec['offsets']
        return tuple((a, words[(j + b) % len(words)], words[(j + c) % len(words)], 0, 0) for j, a in enumerate(words))
    words = list(spec.get('words', ()))
    if 'random' in spec:
        seed, count = spec['random']; rng = random.Random(seed)
        words += [tuple(rng.getrandbits(32) for _ in range(3)) for _ in range(count)]
    return tuple((a, b, c, p, 0) for j, (a, b, c) in enumerate(words)
                 for p in ((0, 1) if spec['predicate'] == 'both' else (1 if spec['predicate'] == 'true' else j & 1,)))
