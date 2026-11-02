import itertools
from functools import cache
from typing import NamedTuple

from sass.decode import ir
from tests.harness import autocase_profiles, autocases
from tests.harness.case import source


class Inputs(NamedTuple):
    vectors: tuple = ()
    values: tuple = ()
    destination: int = 2
    guard: int | None = None


class Entry(NamedTuple):
    request: str
    cls: str
    fields: dict
    inputs: Inputs


def packed(lo, hi): return lo | hi << 16


def mirrored(raw):
    return tuple(packed(x, x ^ (0x8000 if n in (0, len(raw)-1) else 0))
                 for n, x in enumerate(raw))


def vectors(raws, predicate=0, alias=None):
    return tuple((tuple(raw), predicate, alias) for raw in raws)


def flush_inputs(arity):
    for position, magnitude in itertools.product(range(arity), (1, 0x03ff)):
        low = [0, 0] if arity == 2 else [0x3c00, 0x3c00, 0]
        if arity == 3 and position == 2: low[0] = 0
        low[position] = magnitude
        high = list(low)
        high[position] ^= 0x8000
        high[-1] |= 0x8000
        if arity == 2: high[0] |= 0x8000
        if arity == 3 and position == 2: high[0] |= 0x8000
        yield tuple(map(packed, low, high))
    if arity == 2:
        yield mirrored((0x0401, 0x8400))
    else:
        yield mirrored((0x1c00, 0x1c00, 0))
        yield mirrored((0x0400, 0x3bff, 0))
        yield (packed(1, 0x8001), packed(0x7c00, 0x7c00), packed(0, 0x8000))
        yield (packed(0x7c00, 0x7c00), packed(1, 0x8001), packed(0, 0x8000))


def mul_inputs():
    for other in (packed(0x3c00, 0x3c00), packed(0x7c00, 0x7c00), packed(0x7e01, 0xfc01)):
        factors = (packed(0, 0x8001), packed(0x8000, 1)) if other == 0x3c003c00 else (packed(0, 0x8001),)
        for factor in factors:
            yield factor, other
            yield other, factor
    yield packed(0x3c00, 0xbc00), packed(0x3c00, 0x3c00)
    yield packed(0x0400, 0x8400), packed(0x3bff, 0x3bff)


def bf16_inputs():
    values = autocase_profiles.PROFILES['bf16_fma']['values']
    for position in range(3):
        for lo, hi in zip(values[::2], values[1::2]):
            raw = [packed(0x3f80, 0x3f80)] * 2 + [packed(0, 0x8000)]
            if position == 2: raw[0] = packed(0, 0x8000)
            raw[position] = packed(lo, hi)
            yield tuple(raw)
    specials = autocase_profiles.PROFILES['bf16_fma']['witnesses'] + (
        (0x7f7f, 0x3f80, 0x7aff), (0x7f7f, 0x3f80, 0x7b00), (0x7f7f, 0x3f80, 0x7b01),
        (0, 0x7f80, 0), (0x7f80, 0x3f80, 0xff80))
    yield from map(mirrored, specials)


@cache
def entries(arch):
    md = ir.load(arch)
    classes = {k.name: k for k in md.classes if not k.alternate}
    mma = next((name for name in ('hfma2_mma__RRR_norelu', 'hfma2_mma__RRR') if name in classes), None)
    mma_relu = next((name for name in ('hfma2_mma__RRR_relu', 'hfma2_mma_relu__RRR') if name in classes), None)
    out = []

    def add(request, cls, fields, raws=(), *, values=(), predicate=0, alias=None, destination=2, guard=None):
        if cls not in classes: return
        fields = {n: classes[cls].operand_types[n].default if n in ('fmz', 'ftz') and str(v).startswith('no') else v
                  for n, v in fields.items()}
        try:
            autocases.encoded_form(arch, classes[cls], fields)
        except ValueError:
            return
        out.append(Entry(request, cls, fields, Inputs(vectors(raws, predicate, alias), tuple(values), destination, guard)))

    nans = [v for v in autocase_profiles.V16 if v & 0x7c00 == 0x7c00 and v & 0x3ff]
    for sw, ftz in itertools.product(('H0_H0', 'H1_H1'), ('noftz', 'FTZ')):
        add('R1', 'hadd2_F32__RR', {'Ra': 'RZ', 'Ra@negate': 1, 'iswzB_as_C': sw, 'ftz': ftz},
            values=[packed(v, 0x3c00) if sw == 'H0_H0' else packed(0x4000, v) for v in nans])

    for cls, arity, modifier, request in (
            ('hadd2__RR', 2, 'ftz', 'R2'), ('hfma2__RRR', 3, 'fmz', 'R2'), (mma, 3, 'fmz', 'R3')):
        for mode in ('noftz' if modifier == 'ftz' else 'nofmz_hfma2' if cls == 'hfma2_mma__RRR' else 'nofmz', 'FTZ'):
            add(request, cls, {modifier: mode}, tuple(flush_inputs(arity)))

    clamp = ((packed(0xbc00, 0x4000), packed(0x3c00, 0x3c00), 0),
             mirrored((0x8000, 0x3c00, 0x8000)),
             (packed(0x7e01, 0x7c00), packed(0x3c00, 0x3c00), 0), mirrored((0x0400, 0x3bff, 0)))
    for mode in ('nofmz_hfma2' if mma == 'hfma2_mma__RRR' else 'nofmz', 'FTZ'):
        add('R3', mma, dict(fmz=mode, satrelu='SAT'), clamp)
        for p in (0, 1):
            add('R3', mma_relu, dict(fmz=mode, satrelu='RELU'), clamp, predicate=p)
    for mode in ('nofmz', 'FTZ', 'FMZ'):
        fields = {'fmz': mode}
        if 'Dfmt.Afmt.Bfmt' in classes['hmul2__RR'].operand_types:
            fields['Dfmt.Afmt.Bfmt'] = 'F16x2.F16x2.F16x2'
        add('R3', 'hmul2__RR', fields, tuple(mul_inputs()))

    minmax = ('hmnmx2__RRR', 'hmnmx2__RCR', 'hmnmx2__RUR', 'hmnmx2__RCxR', 'hmnmx2__RIR')
    pairs = ((packed(0x3c00, 0x4200), packed(0x4000, 0xbc00)),
             (packed(0, 0x8000), packed(0x8000, 0)),
             (packed(0x7e01, 0xfc01), packed(0xfdff, 0x7fff)))
    for cls, inverted, p in itertools.product(minmax, (0, 1), (0, 1)):
        add('R4', cls, {'Pp@not': inverted}, pairs, predicate=p)
    for cls in minmax:
        if cls not in classes: continue
        k = classes[cls]
        for name in ('Ra', source(k, 'Sb', 'Rb', 'URb')):
            if k.operand_types[name].type not in ('Register', 'UniformRegister', 'C', 'CX'): continue
            for (absolute, negate), p in itertools.product(((1, 0), (0, 1), (1, 1)), (1, 'PT')):
                add('R4', cls, {name+'@absolute': absolute, name+'@negate': negate},
                    ((packed(0xc000, 0x4200), packed(0x3e00, 0xbe00)),), predicate=p)

    for cls, sw, (absolute, negate) in itertools.product(
            ('hmnmx2__RCR', 'hset2__RC', 'hsetp2__RC'), ('H0_H0', 'H1_H1'), ((0, 1), (1, 0), (1, 1))):
        if cls not in classes: continue
        name = 'Sb' if cls.startswith('hmnmx') else 'Sc'
        fields = {name+'@absolute': absolute, name+'@negate': negate,
                  source(classes[cls], 'iswzB', 'iswzB_as_C'): sw}
        if cls.startswith('hset'): fields.update(cmp='GT', bop='AND')
        add('R8', cls, fields, ((packed(0x4100, 0xc100), packed(0xc000, 0x4200)),
                               (packed(0x4100, 0xc100), packed(0x4000, 0xc200))), predicate='PT')

    midpoint = ((0x3c01, 0x3bff, 9), (0x3c02, 0x3bff, 15))
    add('R6', 'hfma2__RRR', {}, tuple(map(mirrored, midpoint)))
    for cls, arity in (('hadd2__RR', 2), ('hfma2__RRR', 3)):
        raw = [(0x7bff, *((0x3c00,) if arity == 3 else ()), x) for x in (0x4b80, 0x4c00, 0x4c40)]
        add('R6', cls, {}, tuple(map(mirrored, raw)))
    add('R6', 'hfma2__RRR', {}, (mirrored((0x3c00, 0x4000, 0xc000)),))
    add('R6', mma, {}, ((packed(1, 0x8001), packed(1, 1), packed(0x7bff, 0x7bff)),))
    add('R6', 'hadd2__RR', {}, ((packed(0x8000, 0), packed(0, 0x8000)),))
    words = tuple(packed(a, b) for a, b in zip(autocase_profiles.V16[::2], autocase_profiles.V16[1::2]))
    for modifier in ('absolute', 'negate'):
        add('R6', 'hadd2__RR', {'Ra@'+modifier: 1, 'Rc@negate': 1},
            tuple((word, 0) for word in words), alias='Rc')
    add('R7', 'hfma2__RRI', {'Ra@negate': 1}, values=words)

    for cls, arity in (('hadd2__RR', 2), ('hfma2__RRR', 3), (mma, 3)):
        add('R9', cls, {}, ((packed(0x4000, 0x4200),) * arity,), alias='__all__', destination=4)
        raw = (packed(0x3c00, 0x4000), packed(0x4200, 0xbc00), packed(0x3800, 0x3c00))[:arity]
        for destination in (4, 6, 8)[:arity]:
            add('R9', cls, {}, (raw,), destination=destination)
    add('R9', 'hadd2_F32__RR', {'Ra': 'RZ', 'Ra@negate': 1, 'iswzB_as_C': 'H1_H1'},
        values=(packed(0x3c00, 0x4000), packed(0x3c00, 0xfdff)), destination=4)
    for old in (0, 1):
        add('R9', 'hsetp2__RR', dict(cmp='GT', bop='AND'),
            ((packed(0x4000, 0x3c00), packed(0x3c00, 0x4000)),), predicate='PT', guard=old)

    values = tuple(dict.fromkeys((*autocase_profiles.PROFILES['conversion_forms']['floats'][16],
                                 *autocase_profiles.V16, 0x3800, 0xb800, 0x3e00, 0xbe00,
                                 *(x | s for x in (0x3bff, 0x3c00, 0x3c01, 0x3fff, 0x4000, 0x4001) for s in (0, 0x8000)))))
    add('R10', 'f2i__Rb_16b', dict(srcfmt='F16', dstfmt='S32', ftz='FTZ', rnd='TRUNC', ntz='NTZ', hsel='H0'),
        values=values)
    for cls, p in (('hfma2__RRR', 0), ('hfma2_relu__RRR', 0), ('hfma2_relu__RRR', 1)):
        if cls not in classes or 'BF16_V2' not in md.enums[classes[cls].operand_types['ofmt'].type]: continue
        add('R12', cls, dict(ofmt='BF16_V2'), tuple(bf16_inputs()), predicate=p)
    return tuple(out)


def cases(arch, fp, family, request=None, discovery=False):
    classes = {k.name: k for k in ir.load(arch).classes}
    seen = set()
    for entry in entries(arch):
        if autocases.FAMILIES[classes[entry.cls].mnemonic] != family or request and request != entry.request:
            continue
        form = autocases.encoded_form(arch, classes[entry.cls], entry.fields)
        for case in autocases.generate(arch, entry.cls, form, fp, input_profile=entry.inputs, discovery=discovery):
            if case.id not in seen:
                seen.add(case.id)
                yield case
