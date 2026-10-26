"""Declarative input corpora shared by every architecture's generated form plan.

The seeds and edge lists preserve the ordinary cases formerly in int_alu/int_simd/bmsk.
Reference calculations and operand placement belong to autocases, not these profiles.
"""
import itertools
import random
from functools import cache

MASK32 = 0xffffffff
VECTORS = ((0, 0, 0, 0, 0), (0, 0, 1, 0, 1), (0, 0, MASK32, 1, 0), (0, 0, 0x80000000, 1, 1),
           (0x80000000, 0x7fffffff, MASK32, 0, 1), (0x7fffffff, 0x80000000, 1, 1, 0),
           (MASK32, 0, 0x12345678, 1, 1), (0x807f01ff, 0x7f80ff00, 0x10203040, 0, 0))

PROFILES = {
    'fchk_boundaries': dict(seed='fchk', mantissas=(0,0x7fffff,1,0x400000), divisor_boundaries=(1,251), differences=(-127,124), stride=16, extra_pairs=((0,0x3f800000),(0x3f800000,0),(0x7fc00000,0x3f800000),(0x3f800000,0x40000000))),
    'floating_forms': {32:(0,0x80000000,0x3f800000,0xc0200000,1,0x7f800000,0x7fc12345,0x3f000001), 64:(0,1<<63,0x3ff0000000000000,0xc004000000000000,1,0x7ff0000000000000,0x7ff8123456789abc,0x3fe0000000000001)},
    # Distinguish source signs, FTZ and saturation at both interval boundaries.
    'floating_source_forms': (0xff800000,0xffc12345,0x80000001,0x007fffff,0x3f800001,0xbf800001),
    'predicate_forms': dict(tables=(0,255,0x96,0xe8,0xca,0x80,0x01,0x7e),masks=(0,1,0x25,0x7f,0x80,0xff,0xffffffff)),
    'arithmetic_edges': dict(vectors=VECTORS + ((0xffffffff,1,0,1,1), (0,0,0,1,1), (0xffffffff,0xffffffff,0xffffffff,0,1), (1,0xffffffff,1,1,0))),
    'imad_carry_words': dict(carry_pairs=True),
    'carry_words': dict(words=((0,0,0),(MASK32,MASK32,MASK32),(0x80000000,0x80000000,0),(MASK32,1,0),(0,MASK32,1)), random=('carry',12), predicate='pair', shifts=(0,1,5,31)),
    'arithmetic_random': dict(random=('int_alu',20), predicate='alternate', shifts='index8'),
    'uniform_arithmetic_random': dict(random=('uniform',12), predicate='alternate', shifts=(3,)),
    'imad_forms': dict(wide_words=((MASK32,MASK32,0xffffffffffffffff),(0x80000000,0x80000000,1<<63),(MASK32,1,0),(0,MASK32,1),(0xffff,0x10000,MASK32),(0x7fffffff,MASK32,1<<32),(1,0x80000000,0),(0,0,0)), random=('imad-forms',4), predicate='both'),
    'imad_negated': dict(negation_edges=(0,1,2,0xffff,0x10000,0x7fffffff,0x80000000,0xfffffffe,MASK32), addends=(0,1,1<<32,0xffffffffffffffff,MASK32,1<<63,5), random=('imad-negate',24), predicate='alternate'),
    'logic_domain': dict(vectors=((0xf0f0f0f0, 0xcccccccc, 0xaaaaaaaa, 0, 0),), selectors=tuple(range(256))),
    'logic_edges': dict(vectors=VECTORS, selectors=(0, 0xff, 0x96, 0xe8, 0xca, 0xf0, 0xcc, 0xaa)),
    'logic_legacy': dict(vectors=((0x81234567, 0x76543210, 0xa5a5f0f0, 1, 0),), selectors=tuple(range(256))),
    'logic_random': dict(random=('int_alu', 20), predicate='true', selectors=(0x96,)),
    'uniform_logic_random': dict(random=('uniform', 12), predicate='true', selectors=(0x96,)),
    'packed_add_minmax': dict(words=(0, MASK32, 0x80007fff, 0x7fff8000, 0x12345678, 0xdeadbeef), random=('vimnmx', 8), offsets=(1, 4), predicate='both'),
    'packed_minmax3': dict(words=(0, MASK32, 0x80007fff, 0x7fff8000, 0x12345678, 0xdeadbeef), random=('vimnmx3', 6), offsets=(2, 4), predicate='both'),
    'packed_minmax': dict(words=(0, MASK32, 0x80007fff, 0x7fff8000, 0x12345678, 0xdeadbeef), random=('vimnmx', 8), packed='minmax'),
    'packed_compare': dict(words=(0, MASK32, 0x80007fff, 0x7fff8000, 0x7f80ff01, 0x12345678), random=('viset', 6), packed='compare'),
    'byte_difference': dict(vectors=(
        (0, 0x807f01ff, 0x7f80ff01, 0, 0),
        (MASK32, 0xff010080, 0x01020304, 0, 0),
        (0x807f01ff, 0x7f80ff01, 0, 0, 0),
        (0xff010080, 0x01020304, MASK32, 0, 0),
        (0x7f80ff01, 0, 0x807f01ff, 0, 0),
        (0x01020304, MASK32, 0xff010080, 0, 0),
        (0x807f01ff, 0xff010080, 0x13579bdf, 0, 0))),
    'absolute': dict(values=(0, 1, 0xffffffff, 0x7fffffff, 0x80000000, 0x80000001),
                     random=('int_alu', 20), component=1),
    'move32': dict(values=(0, 1, 0xffffffff, 0x7fffffff, 0x80000000, 0x01234567, 0xaaaaaaaa),
                   random=('uniform', 12), component=1),
    'move64': dict(values=(0, 1, 0xffffffff, 0x100000000, 0x8000000000000000,
                          0xffffffffffffffff, 0x0123456789abcdef, 0xffffffff00000000)),
    'mask_edges': dict(pairs=tuple(itertools.product((0, 1, 15, 30, 31, 32, 33, 63, 64, 0x7fffffff, 0x80000000, MASK32), repeat=2)),
                       random=(18, 32)),
    'mask_domain': dict(pairs=tuple(itertools.product(range(34), repeat=2))),
    'bits': dict(values=(0, 1, 0x80000000, MASK32, 0x01234567, 0xaaaaaaaa, 0x55555555, 0x80000001, 0x80ff017f),
                 random=('brev_popc', 20), banks=((2, 16), (3, 24))),
    'flo_bits': dict(values=(0, 1, 2, 0x80000000, MASK32, 0xfffffffe, 0x01000000, 0x00ffffff,
                            0x01234567, 0xaaaaaaaa, 0x55555555, 0x80000001, 0x80ff017f),
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
    *((prefix+'__'+shape, {}, 'imad_carry_words') for prefix,shape in
      ((p, 'RRR_RRR') for p in ('imad_hi','imad_hi_x','imad_wide','imad_wide_x'))),
    *((prefix+'__URURUR_URURUR', {}, 'imad_carry_words') for prefix in ('uimad_hi','uimad_hi_x','uimad_wide','uimad_wide_x')),
    ('imad_x__RRR_RRR', {}, 'carry_words'),
    ('uimad_x__URURUR_URURUR', {}, 'carry_words'),
    ('uimad__URURUR_URURUR', {}, 'carry_words'),
    *((cls, {}, 'carry_words') for cls in ('iadd3_noimm__RRR_RRR','iadd3_x_noimm__RRR_RRR','uiadd3__URURUR_URURUR','uiadd3_x__URURUR_URURUR')),
    *((('lea_'+shape+'__RRR_RRR'), {}, 'carry_words') for shape in ('lo_noimm','lo_noimm_x','hi_noimm','hi_noimm_x','hi_noimm_sx32','hi_noimm_sx32_x')),
    *((('ulea_'+shape+'__URURUR_URURUR'), {}, 'carry_words') for shape in ('lo_noimm','lo_noimm_x','hi_noimm','hi_noimm_x','hi_noimm_sx32','hi_noimm_sx32_x')),
    ('iadd3_noimm__RRR_RRR', {'Ra@negate':'unset','Rb@negate':'unset','Rc@negate':'unset'}, 'arithmetic_random'),
    ('iadd3_noimm__RUR_RUR', {}, 'uniform_arithmetic_random'),
    ('lea_lo_noimm__RRR_RRR', {'Ra@negate':'unset','Rb@negate':'unset'}, 'arithmetic_random'),
    ('lea_lo_noimm__RUR_RUR', {}, 'uniform_arithmetic_random'),
    ('ulea_lo_noimm__URURUR_URURUR', {}, 'uniform_arithmetic_random'),
    ('uiadd3__URURUR_URURUR', {}, 'uniform_arithmetic_random'),
    *((cls, {}, 'arithmetic_random') for cls in ('imad__RRR_RRR','imad_hi__RRR_RRR')),
    *((cls, {}, 'uniform_arithmetic_random') for cls in ('imad__RUR_RUR','imad__RRU_RRU','uimad__URURUR_URURUR')),
    *((prefix+'__'+shape, {operand+'@negate':'set'}, 'imad_negated') for prefix,shape,operand in
      (('imad_hi','RRR_RRR','Rc'),('imad_wide','RRR_RRR','Rc'),('uimad_hi','URURUR_URURUR','URc'),('uimad_wide','URURUR_URURUR','URc'))),
    ('lop3_lut__RRR_RRR', {'pop':'POR', 'Pp@not':'unset'}, 'logic_legacy'),
    ('lop3_lut__RRR_RRR', {'pop':'POR', 'Pp@not':'unset'}, 'logic_random'),
    ('ulop3_lut__URURUR_URURUR', {'pop':'POR', 'UPp@not':'unset'}, 'uniform_logic_random'),
    ('bmsk__RRR_RRR', {}, 'mask_domain'),
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
    from tests.harness import autocase_remaining
    if op == 'MUFU' and not autocase_remaining.blocked_reason(op, cls, form): return ['mufu_sincos']
    if op in autocase_remaining.OPS: return ['remaining_forms']
    from tests.harness import autocase_extended, autocase_conversions, autocase_packed_fp, autocase_wide_fp
    if op in ('FHADD2','FHMUL2','FHFMA2'): return ['mixed_pair_forms']
    if op in ('LDC','LDCU','ULDC'): return ['constant_load_forms']
    if op in ('S2R','CS2R','S2UR'): return ['special_register_forms']
    if op == 'QSPC': return ['address_query_forms']
    if op in ('BRA','CALL','RET','EXIT','NOP'): return ['control_flow_forms']
    if op == 'MOV64IUR': return ['move64']
    if op == 'I2I': return ['integer_narrowing']
    if op == 'UI2IP': return ['packed_conversion_forms']
    if op == 'UVIMNMX': return ['packed_minmax']
    if op in ('VIADD', 'IADD'): return ['integer_edges','uniform_arithmetic_random']
    if op in autocase_wide_fp.OPS: return ['binary32_pair_forms'] if op in autocase_wide_fp.PAIR_OPS else ['float_pack_forms']
    if op in ('HSET2','HSETP2'): return ['half_comparison_forms']
    if op in autocase_packed_fp.OPS: return ['packed_conversion_forms'] if op in autocase_packed_fp.PACK_OPS else ['half_forms']
    if op in autocase_conversions.OPS: return ["conversion_forms","conversion_boundaries"]
    if op in autocase_extended.PREDICATE_OPS: return ['predicate_forms']
    if op=='FCHK': return ['floating_forms','fchk_boundaries']
    if op in autocase_extended.FLOAT_OPS: return ['floating_forms']
    if op in ('IMAD','UIMAD'): out = ['arithmetic_edges','imad_forms']
    elif op in ('IADD3','UIADD3','LEA','ULEA'): out = ['arithmetic_edges']
    elif op in ('LOP3', 'ULOP3'): out = ['logic_domain', 'logic_edges']
    elif op == 'VIADDMNMX': out = ['integer_edges', 'packed_add_minmax']
    elif op == 'VIMNMX3': out = ['integer_edges', 'packed_minmax3']
    elif op == 'VIMNMX': out = ['integer_edges', 'packed_minmax']
    elif op == 'VABSDIFF4': out = ['integer_edges', 'byte_difference']
    elif op == 'VISET': out = ['integer_edges', 'packed_compare']
    elif op == 'IABS': out = ['absolute']
    elif op in ('MOV', 'UMOV', 'R2UR'): out = ['move64' if form.get('size') == '64' else 'move32']
    elif op == 'BMSK': out = ['mask_edges']
    elif op in ('FLO', 'UFLO'): out = ['flo_bits']
    elif op in ('BREV', 'POPC', 'UBREV', 'UPOPC'): out = ['bits']
    elif op in ('SGXT', 'USGXT'): out = ['sgxt']
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
        out += [tuple(rng.getrandbits(32) for _ in range(3))[spec['component']] if 'component' in spec
                else rng.getrandbits(32) for _ in range(count)]
    return tuple(out)


@cache
def vectors(name):
    spec = PROFILES[name]
    if 'vectors' in spec: return spec['vectors']
    if spec.get('carry_pairs'):
        return tuple((a,b,(b<<32)|c,p,q) for a,b,c,p,q in vectors('carry_words'))
    if 'wide_words' in spec or 'negation_edges' in spec:
        words = list(spec.get('wide_words', ()))
        if 'negation_edges' in spec:
            addends = spec['addends']
            words = [(a,b,addends[i*5 % len(addends)]) for i,(a,b) in enumerate(itertools.product(spec['negation_edges'], repeat=2))]
            words += [(a,a,d) for a in (0,1,MASK32) for d in addends]
        seed,count = spec['random']; rng = random.Random(seed)
        words += [(rng.getrandbits(32),rng.getrandbits(32),rng.getrandbits(64)) for _ in range(count)]
        return tuple((a,b,c,p,0) for j,(a,b,c) in enumerate(words) for p in ((0,1) if spec['predicate']=='both' else (j&1,)))
    if spec.get('predicate') == 'pair':
        seed,count = spec['random']; rng = random.Random(seed)
        words = [*spec['words'], *(tuple(rng.getrandbits(32) for _ in range(3)) for _ in range(count))]
        return tuple((a,b,c,p,q) for a,b,c in words for p,q in itertools.product((0,1),repeat=2))
    if 'packed' in spec:
        words = values(name)
        return tuple((a, words[(j + 3) % len(words)] if spec['packed'] == 'minmax' else
                      a if j % 4 == 0 else words[(j * 5 + 1) % len(words)], 0, pred, 0)
                     for j, a in enumerate(words) for pred in ((0, 1) if spec['packed'] == 'minmax' else (0,)))
    if 'pairs' in spec:
        pairs = list(spec['pairs'])
        if 'random' in spec:
            seed, count = spec['random']; rng = random.Random(seed)
            pairs += [(rng.getrandbits(32), rng.getrandbits(32)) for _ in range(count)]
        return tuple((a, b, 0, 0, 0) for a, b in pairs)
    if 'offsets' in spec:
        words = values(name); b, c = spec['offsets']
        return tuple((a, words[(j + b) % len(words)], words[(j + c) % len(words)], pred, 0)
                     for j, a in enumerate(words) for pred in ((0, 1) if spec.get('predicate') == 'both' else (0,)))
    words = list(spec.get('words', ()))
    if 'random' in spec:
        seed, count = spec['random']; rng = random.Random(seed)
        words += [tuple(rng.getrandbits(32) for _ in range(3)) for _ in range(count)]
    return tuple((a, b, c, p, 0) for j, (a, b, c) in enumerate(words)
                 for p in ((0, 1) if spec['predicate'] == 'both' else (1 if spec['predicate'] == 'true' else j & 1,)))


@cache
def fchk_pairs():
    """Original FCHK exponent-boundary and seeded GPU corpus, shared by profiles and fixtures."""
    spec=PROFILES['fchk_boundaries']; rng=random.Random(spec['seed']); out=[]
    mantissas=spec['mantissas']; signs=(0,1<<31)
    for ea in range(256):
        boundaries=(*spec['divisor_boundaries'], *(ea+d for d in spec['differences']))
        for eb in sorted({x for t in boundaries for x in (t,t+1) if 0<=x<256}):
            n=len(out)
            out.append((signs[n%2] | ea<<23 | mantissas[n%4], signs[n//2%2] | eb<<23 | mantissas[(n+1)%4]))
    return tuple(out+[(rng.getrandbits(32),rng.getrandbits(32)) for _ in range(-len(out)%128+128)])


PROFILES["conversion_forms"] = {
    "floats": {
        16: (0,0x8000,1,0x3e00,0xbe00,0x7bff,0x7c00,0x7e01),
        32: (0,0x80000000,1,0x3fc00000,0xbfc00000,0x4f000000,0x7f800000,0x7fc12345),
        64: (0,1<<63,1,0x3ff8000000000000,0xbff8000000000000,0x43f0000000000000,0x7ff0000000000000,0x7ff8123456789abc),
    },
    "integers": (0,1,0xffffffffffffffff,0x8000000000000000,0x80000000,0x01000001,0x8001,0x81),
    "filler": 0xa5a5a5a5,
}


PROFILES['packed_conversion_forms'] = {
    'floats': (0,0x80000000,0x3fc00000,0xbfc00000,0x43000000,0xc3000000,0x7f800000,0x7fc12345),
    'integers': (0,1,0xffffffff,0x7fffffff,0x80000000,0x7f,0x80,0x12345678),
    'preserved': 0xa53c91e7,
}
PROFILES['half_forms'] = {
    'F16': (0,0x80000000,0x3c004000,0xbc00c000,0x00018001,0x7bfffbff,0x7c003c00,0x3c007e01),
    'widening': (0x00010001,0x80010001,0x03ff0400,0x040003ff,0x35553aaa,0x34003800,
                 0x00003c00,0x00008000,0x0000bc00,
                 0x7bff3c00,0x3c007c00,0x7e003c00,0x7c00fc00),
    'BF16': (0,0x80000000,0x3f804000,0xbf80c000,0x00018001,0x7f7fff7f,0x7f803f80,0x3f807fc1),
    'E6M9': (0,0x80000000,0x3e004000,0xbe00c000,0x00018001,0x7dfffdff,0x7e003e00,0x3e007f01),
}


PROFILES["conversion_boundaries"] = {'when': 'unmodified_register_noftz',
 'floats': {16: (0,
                 32768,
                 1,
                 32769,
                 1023,
                 14336,
                 47104,
                 15872,
                 48640,
                 16640,
                 49408,
                 14848,
                 46080,
                 31743,
                 64511,
                 31744,
                 64512,
                 32256,
                 64769,
                 25600,
                 25599,
                 22528),
            32: (0,
                 2147483648,
                 1,
                 2147483649,
                 8388607,
                 1056964608,
                 3204448256,
                 1069547520,
                 3217031168,
                 1075838976,
                 3223322624,
                 1061158912,
                 3196059648,
                 1258291201,
                 2139095039,
                 4286578687,
                 2139095040,
                 4286578688,
                 2143289344,
                 4286578689,
                 1325400064,
                 3472883713,
                 1602224127,
                 3741319168),
            64: (0,
                 9223372036854775808,
                 1,
                 9223372036854775809,
                 4602678819172646912,
                 13826050856027422720,
                 4609434218613702656,
                 13832806255468478464,
                 4612811918334230528,
                 13836183955189006336,
                 9218868437227405311,
                 9218868437227405312,
                 18442240474082181120,
                 9219994337134247936,
                 18444492273895866369,
                 4746794007248502784,
                 13970166044105375744,
                 4895412794951729152,
                 3931642474694443009,
                 13155014511549218817,
                 5183643170835005439,
                 5183643170835005440,
                 4679237813814689792,
                 4607182419068452864)},
 'integers': (0,
              1,
              4294967295,
              2147483647,
              2147483648,
              16777217,
              16777219,
              4294967167,
              18446744073709551615,
              9223372036854775808,
              9223372036854775809,
              9007199254740993,
              18446744073709551614,
              9223372586610589696,
              65519,
              65520,
              65520,
              32768,
              128,
              127),
 'f2f16': (0,
           0,
           1,
           1,
           65535,
           0,
           0,
           0,
           0,
           0,
           0,
           0,
           0,
           1,
           65535,
           65535,
           0,
           0,
           0,
           1,
           0,
           1,
           65535,
           0,
           31744,
           64512,
           32257,
           64769,
           1,
           32769,
           31743),
 'fixture_integers': (0,
                      1,
                      4294967295,
                      2147483647,
                      2147483648,
                      16777217,
                      16777219,
                      4294967167,
                      18446744073709551615,
                      9223372036854775808,
                      9223372036854775809,
                      9007199254740993,
                      18446744073709551614,
                      9223372586610589696)}


PROFILES['binary32_pair_forms'] = {
    'words': (0,0x80000000,1,0x3f800000,0xc0200000,0x7f7fffff,0x7f800000,0x3f000001),
}
PROFILES['float_pack_forms'] = {
    'words': (0,0x80000000,0x3f800000,0xbf800000,0x3f801000,0x477ff000,0x7f800000,0x7fc12345),
}

PROFILES['half_comparison_forms'] = dict(inputs='half_forms', selection='half_comparison_inputs.json', supplement='unmasked_ftz_witness')


PROFILES['mixed_pair_forms'] = {
    'F32': (0x3f800000,0xc0200000,0x3f000001,0x40800000,0x38800000,0x477fe000),
    'F16': (0x3c00,0xc100,0x3801,0x4400,0x0400,0x7bff),
    'BF16': (0x3f80,0xc020,0x3f01,0x4080,0x0080,0x477f),
}
PROFILES['constant_load_forms'] = {
    'words': (0,0xffffffffffffffff,0x800180ff,0x89abcdef01234567,0x12345678,0x0000000200000001),
    'wide_words': (0, (1 << 128)-1, 0x800180ff, 0xfedcba98765432100123456789abcdef,
                   0x12345678, 0x00000004000000030000000200000001),
    'banks': (2,3), 'offsets': (-16,16),
}
PROFILES['special_register_forms'] = {
    'sources': ('SR_LANEID','SR_CTAID.X','SRZ'),
    'words': (0,0x89abcdef,0xffffffff), 'high': 0x12345678,
}
PROFILES['control_flow_forms'] = {
    'predicates': (0,1), 'offsets': (-64,0,32), 'target': 0x4560, 'pc': 0x1000,
    'sentinels': {'R8':0x12345678,'R9':0x89abcdef,'P1':1,'UR8':0xa53c91e7},
}
PROFILES['address_query_forms'] = {
    'ranges': {'global': [[0x100000,0x100100]], 'shared': [[0x200,0x300]],
               'local': [[0x400,0x500]], 'shared_cluster': [[0x600,0x700]]},
    'queries': ('global','shared','local'), 'positions': (0,1,0xff),
    'offsets': (-16,16),
}


PROFILES['remaining_forms'] = {
    'words': (0, 0x89abcdef, 0xffffffff),
    'wide_words': (0, 0xfedcba98765432100123456789abcdef, (1 << 128) - 1),
}
PROFILES['integer_narrowing'] = dict(values=(
    0, 1, -1, 127, 128, -128, -129, 255, 256, 32767, 32768,
    -32768, -32769, 65535, 65536, 0x7fffffff, -0x80000000,
    0x12345678, -0x01234568))
PROFILES['mufu_sincos'] = {'words': (
    0, 0x80000000, 1, 0x807fffff, 0x00800000,
    0x32ffffff, 0x33000000, 0x33000001, 0xb3000000,
    0x38ffe000, 0x39000000, 0x39002000, 0x3daaaaab,
    0x3dcccccd, 0x3e000000, 0x3e7fffff, 0x3e800000, 0x3e800001,
    0x3effffff, 0x3f000000, 0x3f000001, 0x3f400000, 0xbf800000,
    0x4a7fffff, 0x4a800000, 0x4a800001, 0x4b000000, 0x7f7fffff,
    0x7f800000, 0xff800000, 0x7fc00000, 0xff800001)}
