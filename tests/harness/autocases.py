"""Reference adapters for generated shared-case form plans. No implementation-derived outputs."""
import hashlib, json
from typing import NamedTuple
from functools import cache
from sass.md import parse
from sass.paths import ROOT
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness.case import Case, Cases, READABLE_FLOATS, put, source, cx_source
from tests.harness import sampling
from tests.refs import integer
from tests.harness import autocase_profiles, autocase_extended, autocase_conversions, autocase_packed_fp, autocase_wide_fp, autocase_integer_extra, autocase_system, autocase_mixed_fp, autocase_remaining

PLANS = ROOT / 'tests/cases/generated'
OPS = {'BREV', 'POPC', 'FLO', 'SGXT', 'PRMT', 'UPRMT', 'IABS', 'MOV', 'UMOV', 'BMSK', 'UBREV', 'UPOPC', 'USGXT'}
INTEGER_OPS = {'ISETP', 'UISETP', 'IMNMX', 'SEL', 'USEL', 'IDP', 'SHF', 'USHF', 'LOP3', 'ULOP3'}
CARRY_OPS = {'IADD3', 'UIADD3', 'LEA', 'ULEA', 'IMAD', 'UIMAD'}
INTEGER_OPS |= CARRY_OPS
PACKED_OPS = {'VIMNMX', 'VISET', 'VIADDMNMX', 'VIMNMX3'}
INTEGER_OPS |= PACKED_OPS
FAMILIES = {op: 'carry' if op in CARRY_OPS else 'int_simd' if op in PACKED_OPS | {'IDP'} else 'bmsk' if op == 'BMSK' else 'int_alu' for op in OPS | INTEGER_OPS}
OPS |= autocase_remaining.OPS | autocase_system.OPS | autocase_mixed_fp.OPS | autocase_integer_extra.OPS | INTEGER_OPS | autocase_extended.OPS | autocase_conversions.OPS | autocase_packed_fp.OPS | autocase_wide_fp.OPS
FAMILIES.update(autocase_remaining.FAMILIES)
FAMILIES.update(autocase_system.FAMILIES)
FAMILIES.update(autocase_mixed_fp.FAMILIES)
FAMILIES.update(autocase_integer_extra.FAMILIES)
FAMILIES.update(autocase_extended.FAMILIES)
FAMILIES.update(autocase_packed_fp.FAMILIES)
FAMILIES.update(autocase_wide_fp.FAMILIES)
FAMILIES.update({op:"conversions" for op in autocase_conversions.OPS})
OWNED = OPS - {'FLO'}  # FLO's partially modelled predicate outputs retain explicit PT-only cases.


class Unsupported(ValueError): pass


class Pending(NamedTuple):
    """A filtered, reference-backed input description; no instruction has been encoded."""
    cls: str
    fields: dict
    request: dict
    expected: dict
    hardware: dict | None
    modifiers: dict

    @property
    def id(self):
        request = {k:v for k,v in self.request.items() if k != 'inspect_pcode'}
        return self.cls + '-' + hashlib.sha256(json.dumps((self.fields, request), sort_keys=True).encode()).hexdigest()[:16]

    @property
    def modifier_form(self): return self.cls, tuple(sorted(self.modifiers.items()))


def generate(arch, cls, form, fp='exact', profiles=None, native=True, sample=sampling.COMPLETE):
    """Choose reference-backed input descriptions before encoding the selected cases."""
    sample = sampling.percentage(sample)
    c = candidates(arch, cls, form, fp, profiles, native)
    chosen = sampling.select(c, arch, sample)
    out, identities = [], {}
    for candidate in chosen:
        if isinstance(candidate, Pending):
            word = checked_word(arch, candidate.cls, candidate.fields, candidate.modifiers)
            case = Case(candidate.cls, dict(candidate.request, word=word), candidate.expected, candidate.hardware)
            if sample < sampling.COMPLETE:
                if set(sampling.buckets(arch, candidate)) != set(sampling.buckets(arch, case)):
                    raise Unsupported('encoded case changed its sampling form/backend eligibility')
        else: case = candidate
        identities[case.id] = candidate.id
        out.append(case)
    c[:] = out
    c.deferred = False
    c.sampling_ids = identities
    return finish(c, False)


def checked_word(arch, cls, fields, form, imad_alias=False):
    word = encoding.encode(arch, cls, **fields)
    decoded = pydecode.decode(arch, bytes.fromhex(word))
    found = settings(arch, decoded)
    alias = imad_alias and cls == 'imad__RsIR_RIR' and decoded.klass.name == 'imad_pseudo__RsIR_RIR'
    if alias: found = {n:v for n,v in found.items() if n != 'pseudo_opcode'}
    if (decoded.klass.name != cls and not alias) or found != form:
        raise Unsupported('encoded instruction does not preserve the requested class/form')
    return word


def candidates(arch, cls, form, fp='exact', profiles=None, native=True):
    """Describe reference-backed inputs for a full form, deferring encoding."""
    c = Cases(arch, fp)
    c.deferred = True
    if cls not in c.classes: raise Unsupported('alternate or absent instruction class')
    k = c.classes[cls]
    if k.mnemonic not in OPS:
        raise Unsupported('needs a reference/operand adapter')
    profiles = autocase_profiles.names(k.mnemonic, cls, form) if profiles is None else profiles
    if not profiles or any(name not in autocase_profiles.PROFILES for name in profiles):
        raise Unsupported('empty or unknown input profile')
    if k.mnemonic in autocase_remaining.OPS:
        return finish(autocase_remaining.generate(c,k,form,profiles),native)
    if k.mnemonic in autocase_system.OPS:
        return finish(autocase_system.generate(c,k,form,profiles),native)
    if k.mnemonic in autocase_mixed_fp.OPS:
        if not native: raise Unsupported('mixed floating runtime fallback remains unresolved')
        return finish(autocase_mixed_fp.generate(c,k,form,profiles),native)
    if k.mnemonic in autocase_integer_extra.OPS:
        if not native: raise Unsupported('additional integer runtime fallback is unresolved')
        return finish(autocase_integer_extra.generate(c,k,form,profiles),native)
    if k.mnemonic in autocase_wide_fp.OPS:
        if not native: raise Unsupported('wide floating runtime fallback cannot execute the full numerical profile')
        return finish(autocase_wide_fp.generate(c,k,form,profiles),native)
    if k.mnemonic in autocase_packed_fp.OPS:
        if not native: raise Unsupported('packed runtime fallback cannot execute the full numerical profile')
        return finish(autocase_packed_fp.generate(c,k,form,profiles),native)
    if k.mnemonic in autocase_conversions.OPS:
        if not native: raise Unsupported('conversion runtime fallback cannot execute the full numerical profile')
        return finish(autocase_conversions.generate(c,k,form,profiles),native)
    if k.mnemonic in autocase_extended.OPS: return finish(autocase_extended.generate(c,k,form,profiles),native)
    if k.mnemonic in ('IABS', 'MOV', 'UMOV', 'BMSK'):
        return finish(scalar_cases(c, k, form, profiles), native)
    if k.mnemonic in CARRY_OPS: return finish(carry_cases(c, k, form, profiles), native)
    if k.mnemonic in ('LOP3', 'ULOP3'): return finish(logic_cases(c, k, form, profiles), native)
    if k.mnemonic in PACKED_OPS: return finish(packed_cases(c, k, form, profiles), native)
    if k.mnemonic in INTEGER_OPS: return finish(integer_cases(c, k, form, profiles), native)
    profile = 'permutation' if k.mnemonic in ('PRMT', 'UPRMT') else 'sgxt' if k.mnemonic in ('SGXT', 'USGXT') else 'bits'
    if profiles != [profile]: raise Unsupported('incompatible input profiles')
    src = source(k, 'Sb') if 'Sb' in k.operand_types else source(k, 'Rb', 'URb')
    dest = source(k, 'Rd', 'URd')
    if src is None or dest is None:
        raise Unsupported('unsupported operand layout')
    allowed = {'fmt', 'sh'} if k.mnemonic == 'FLO' else {'fmt', 'cw'} if k.mnemonic in ('SGXT', 'USGXT') else {'pmode', 'idx'} if k.mnemonic in ('PRMT', 'UPRMT') else set()
    allowed |= {src + '@invert'}
    if set(form) - allowed:
        raise Unsupported('unhandled modifiers: ' + ', '.join(sorted(set(form) - allowed)))
    if form.get('fmt', 'U32') not in ('S32', 'U32'):
        raise Unsupported('unsupported integer format')
    if k.mnemonic in ('PRMT', 'UPRMT'):
        mode = form.get('pmode', form.get('idx', 'IDX'))
        if mode not in ('IDX', 'F4E', 'B4E', 'RC8', 'RC16', 'ECL', 'ECR'): raise Unsupported('unknown PRMT mode')
        spec = autocase_profiles.PROFILES[profile]
        for a, b in spec['data']:
            for selector in spec['idx' if mode == 'IDX' else 'other']:
                for placement in spec['destinations']:
                    s = Inputs(c, k, form)
                    s.operand((src,), selector, 6)
                    s.operand(('Ra', 'URa'), a, 4); s.operand(('Sc', 'Rc', 'URc'), b, 8)
                    expected = {s.output((dest,), 4 if placement == 'alias_left' else 2):integer.prmt(a, selector, b, mode)}
                    s.add(expected, form)
    else:
        spec = autocase_profiles.PROFILES[profile]
        for value in autocase_profiles.values(profile):
            for width in (spec['widths'] if k.mnemonic in ('SGXT', 'USGXT') else (None,)):
                banks = spec.get('banks', ((2, 16),)) if k.operand_types[src].type == 'C' else ((2, 16),)
                for bank in banks:
                    s = Inputs(c, k, form, bank=bank)
                    operand = value if width is None else width
                    s.operand((src,), operand, 6)
                    operand = (~operand & 0xffffffff) if form.get(src + '@invert') == 'set' else operand
                    if k.mnemonic in ('BREV', 'UBREV'): result = integer.brev(operand)
                    elif k.mnemonic in ('POPC', 'UPOPC'): result = operand.bit_count()
                    elif k.mnemonic == 'FLO':
                        if form.get('sh', 'nosh') not in ('SH', 'nosh'): raise Unsupported('unknown FLO mode')
                        s.fields['Pu'] = 'PT'
                        result = integer.flo(operand, form.get('sh') == 'SH', form.get('fmt') == 'S32')
                    else:
                        if form.get('cw', 'C') not in ('C', 'W'): raise Unsupported('unknown SGXT mode')
                        s.operand(('Ra', 'URa'), value, 4)
                        result = integer.sgxt(value, operand, form.get('cw') == 'W', form.get('fmt') == 'S32')
                    s.add({s.output((dest,), 2):result}, form)
    return finish(c, native)


def finish(c, native):
    """Union overlapping profiles, rejecting inconsistent expectations for the same scenario."""
    if not c: raise Unsupported("input profiles produced no cases")
    unique = {}
    for case in c:
        if native:
            # Explicit op/effect bounds cover native memory ordering and an empty NOP.
            guard = {} if isinstance(case.expected.get('__max_ops'),dict) else {'__native':True}
            case = case._replace(expected={**case.expected, **guard}, request={**case.request, 'inspect_pcode':True})
        identity = case.id
        if identity in unique and unique[identity] != case: raise ValueError(f'{identity}: conflicting generated expectations')
        unique[identity] = case
    c[:] = unique.values()
    return c


class Inputs:
    """Place logical operands without confusing CX descriptor children with data operands."""
    def __init__(self, c, k, form, bank=None):
        self.c, self.k = c, k
        self.fields = {n: int(v == 'set') if '@' in n else v for n, v in form.items()}
        self.registers, self.memory = {}, []
        self.bank = bank

    def operand(self, names, value, reg, size=4):
        name = source(self.k, *names)
        if name is None: raise Unsupported('unsupported operand layout: ' + '/'.join(names))
        kind = self.k.operand_types[name].type
        if kind == 'CX':
            fs, rs, ms = cx_source(self.c.arch, self.k, name, value, ur=10, size=size)
            self.fields.update(fs); self.registers.update(rs); self.memory.extend(ms)
        else:
            # Signed immediates encode the signed interpretation of the same input bits.
            value = integer.sext(value, 32) if kind == 'SImm' else value
            if not put(self.k, name, value, self.fields, self.registers, self.memory, reg,
                       bank=self.bank or (2, reg * 4), imm=('SImm', 'UImm'), size=size):
                raise Unsupported('unsupported source type: ' + kind)
        return name

    def predicate(self, names, value, reg):
        name = source(self.k, *names)
        if name is None: raise Unsupported('unsupported predicate layout')
        self.fields[name] = reg
        self.registers[('UP' if self.k.operand_types[name].type == 'UniformPredicate' else 'P') + str(reg)] = value
        return name

    def output(self, names, reg):
        name = source(self.k, *names)
        if name is None: raise Unsupported('unsupported output layout')
        kind = self.k.operand_types[name].type
        prefix = {'Register':'R', 'UniformRegister':'UR', 'Predicate':'P', 'UniformPredicate':'UP'}.get(kind)
        if prefix is None: raise Unsupported('unsupported output type: ' + kind)
        self.fields[name] = reg
        return prefix + str(reg)

    def add(self, expected, form, imad_alias=False, portable=False, **extras):
        if self.c.fp == 'readable' and not portable and self.k.mnemonic in READABLE_FLOATS: return
        hardware = extras.pop('hardware', None)
        observe = extras.pop('observe', None)
        request = self.c.request(None, self.registers, expected, observe, dict(memory=self.memory, **extras))
        # These paths need the actual decoded class/environment to classify them.
        # They are small; keep their existing eager validation and alias behavior.
        eager = (imad_alias and self.k.name == 'imad__RsIR_RIR') or (hardware or {}).get('adapter') == 'memory'
        if getattr(self.c, 'deferred', False) and not eager:
            self.c.append(Pending(self.k.name, dict(self.fields), request, expected, hardware, dict(form)))
        else:
            word = checked_word(self.c.arch, self.k.name, self.fields, form, imad_alias)
            self.c.append(Case(self.k.name, dict(request, word=word), expected, hardware))


def scalar_cases(c, k, form, profiles):
    """Bit-mask, absolute-value and bit-preserving moves, including register pairs."""
    op = k.mnemonic
    allowed = {'cw'} if op == 'BMSK' else {'size'} if op in ('MOV', 'UMOV') else set()
    if set(form) - allowed: raise Unsupported('unhandled modifiers: ' + ', '.join(sorted(set(form) - allowed)))
    if op == 'BMSK':
        if form.get('cw') not in ('C', 'W'): raise Unsupported('unknown BMSK mode')
        for j, (a, b, *_) in enumerate(vector for profile in profiles for vector in autocase_profiles.vectors(profile)):
            s = Inputs(c, k, form)
            s.operand(('Ra',), a, 4); s.operand(('Sb', 'Rb', 'URb'), b, 6)
            s.add({s.output(('Rd',), (4, 6, 2)[j % 3]):integer.bmsk(a, b, form['cw'])}, form)
    else:
        width = 64 if form.get('size') == '64' else 32
        if form.get('size', '32') not in ('32', '64'): raise Unsupported('unknown move width')
        src = source(k, 'Sb', 'Rb', 'URb')
        if src is None: raise Unsupported('unsupported move/absolute source')
        if width == 64 and k.operand_types[src].type not in ('Register', 'UniformRegister'):
            raise Unsupported('64-bit moves need a register-pair source')
        for j, value in enumerate(value for profile in profiles for value in autocase_profiles.values(profile)):
            s = Inputs(c, k, form)
            s.operand((src,), value, 4, size=width // 8)
            if 'PixMaskU04' in k.operand_types: s.fields['PixMaskU04'] = 15
            rd = 4 if j & 1 else 2
            name = s.output(('Rd', 'URd'), rd)
            result = integer.iabs(value) if op == 'IABS' else integer.mov(value, width)
            expected = {name:result & 0xffffffff}
            if width == 64:
                prefix = 'UR' if name.startswith('UR') else 'R'
                expected[prefix + str(rd + 1)] = result >> 32
            s.add(expected, form)
    return c


@cache
def operand_sizes(arch):
    """Operand geometry comes from the machine description, including its documented fixes."""
    return {r['cls']:r['preds'] for r in parse.load_classes(parse.MD / arch)}


def carry_cases(c, k, form, profiles):
    op = k.mnemonic.lstrip('U')
    a_name, b_name, c_name = source(k, 'Ra', 'URa'), source(k, 'Sb', 'Rb', 'URb'), source(k, 'Sc', 'Rc', 'URc')
    p, q = source(k, 'Pp', 'UPp'), source(k, 'Pq', 'UPq')
    extended = 'X' in k.operand_types
    suffix = '@invert' if extended else '@negate'
    operands = (a_name, b_name, c_name) if op == 'IADD3' else (a_name, b_name) if op == 'LEA' else (c_name,)
    allowed = {n + suffix for n in operands if n} | {n + '@not' for n in (p, q) if n} | {'X'}
    allowed |= {'hilo', 'sx32'} if op == 'LEA' else {'fmt'} if op == 'IMAD' else set()
    if set(form) - allowed: raise Unsupported('unhandled arithmetic modifiers: ' + ', '.join(sorted(set(form)-allowed)))
    if form.get('X', 'X') != 'X': raise Unsupported('unknown extended arithmetic mode')
    if op == 'IMAD' and form.get('fmt') not in ('U32', 'S32'): raise Unsupported('unknown IMAD signedness')
    if op == 'LEA' and (form.get('hilo') not in ('LO', 'HI') or form.get('sx32', 'SX32') != 'SX32'):
        raise Unsupported('unknown LEA word selection')
    mode = 'WIDE' if '_wide' in k.name else 'HI' if '_hi' in k.name else 'LO'
    sizes = operand_sizes(c.arch)
    geometry = sizes[k.name.replace('Cx', 'C')] if 'Cx' in k.name else sizes[k.name]
    c_width = geometry.get('ISRC_C_SIZE', 32) if op == 'IMAD' else 32
    if c_width not in (32,64): raise Unsupported('unsupported arithmetic addend width')
    index = 0
    for profile in profiles:
        spec = autocase_profiles.PROFILES[profile]
        for j, (a, b, acc, pred, prior) in enumerate(autocase_profiles.vectors(profile)):
            paired = spec.get('predicate') == 'pair' or spec.get('carry_pairs')
            if paired and (not extended and pred or (op != 'IADD3' or not extended) and prior): continue
            if spec.get('predicate') == 'both' and not extended and pred: continue
            if op == 'IMAD': acc &= (1 << c_width) - 1
            else: acc &= 0xffffffff
            shifts = (j % 8,) if spec.get('shifts') == 'index8' else spec.get('shifts', (0, 1, 5, 31))
            for shift in (shifts if op == 'LEA' else (0,)):
                s = Inputs(c, k, form)
                s.operand((a_name,), a, 4); s.operand((b_name,), b, 6)
                if c_name and (op != 'LEA' or form['hilo'] == 'HI' and form.get('sx32') != 'SX32'):
                    s.operand((c_name,), acc, 8, size=c_width // 8 if op == 'IMAD' else 4)
                effective = []
                for n, value, reg in ((p, pred, 0), (q, prior, 1)):
                    if n:
                        s.predicate((n,), value, reg)
                        effective.append(value ^ (form.get(n + '@not') == 'set'))
                    else: effective.append(0)
                flips = tuple(form.get(n + suffix) == 'set' for n in operands)
                if op == 'IADD3': result, pu, pv = integer.iadd3(a, b, acc, flips, extended, *effective)
                elif op == 'LEA':
                    s.fields['scaleU5'] = shift
                    result, pu = integer.lea(a, b, acc, shift, form['hilo']=='HI', form.get('sx32')=='SX32', flips, extended, effective[0])
                else:
                    result, pu = integer.imad(a,b,acc,mode,form['fmt']=='S32',flips[0] and not extended,flips[0] and extended,effective[0],addend_width=c_width)
                rd = (4, 6, 8, 2)[index % 4]
                expected = {s.output(('Rd','URd'),rd):result & 0xffffffff}
                if op == 'IMAD' and mode == 'WIDE':
                    expected[('UR' if k.mnemonic.startswith('U') else 'R') + str(rd+1)] = result >> 32
                if pu is not None and source(k, 'Pu','UPu'): expected[s.output(('Pu','UPu'),0)] = pu
                if op == 'IADD3' and source(k, 'Pv','UPv'): expected[s.output(('Pv','UPv'),1)] = pv
                s.add(expected, form, imad_alias=op == 'IMAD')
                index += 1
    return c


def logic_cases(c, k, form, profiles):
    p = source(k, 'Pp', 'UPp')
    if p is None: raise Unsupported('unsupported logic predicate layout')
    if set(form) - {'pop', p + '@not'}: raise Unsupported('unhandled logic modifiers')
    if form.get('pop') not in ('POR', 'PAND'): raise Unsupported('unsupported logic predicate combiner')
    index = 0
    for profile in profiles:
        spec = autocase_profiles.PROFILES[profile]
        if not spec.get('selectors'): raise Unsupported('logic profiles need truth-table selectors')
        for a, b, acc, pred, _ in autocase_profiles.vectors(profile):
            for table in spec['selectors']:
                s = Inputs(c, k, form)
                s.operand(('Ra', 'URa'), a, 4); s.operand(('Sb', 'Rb', 'URb'), b, 6)
                s.operand(('Sc', 'Rc', 'URc'), acc, 8)
                s.predicate((p,), pred, 0)
                s.fields['imm8'] = table
                result, output = integer.lop3(a, b, acc, table, pred ^ (form.get(p + '@not') == 'set'), form['pop'])
                expected = {s.output(('Rd', 'URd'), (4, 6, 8, 2)[index % 4]):result,
                            s.output(('Pu', 'UPu'), index % 2):output}
                s.add(expected, form)
                index += 1
    return c


def packed_cases(c, k, form, profiles):
    op = k.mnemonic
    srcb = source(k, 'Sb', 'Rb', 'URb')
    allowed = {'fmt', 'icmp'} if op == 'VISET' else {'fmt', 'relu', 'Pp@not'}
    if op == 'VIADDMNMX' and srcb: allowed.add(srcb + '@negate')
    if set(form) - allowed: raise Unsupported('unhandled modifiers: ' + ', '.join(sorted(set(form) - allowed)))
    formats = {'U32':(32, False), 'S32':(32, True), 'U16x2':(16, False),
               'S16x2':(16, True), 'U8x4':(8, False), 'S8x4':(8, True)}
    if form.get('fmt') not in formats: raise Unsupported('unsupported packed integer format')
    width, signed = formats[form['fmt']]
    if op != 'VISET':
        if any(n in k.operand_types for n in ('Pu', 'Pv')):
            raise Unsupported('unresolved packed min/max predicate outputs')
        if form.get('relu') not in ('norelu', 'RELU') or (not signed and form['relu'] == 'RELU'):
            raise Unsupported('unsupported packed min/max RELU mode')
    elif form.get('icmp') not in integer.COMPARISONS: raise Unsupported('unsupported packed comparison')
    destinations = (4, 6, 8, 2) if op in ('VIADDMNMX', 'VIMNMX3') else (4, 6, 2)
    for j, (a, b, acc, pred, _) in enumerate(vector for profile in profiles for vector in autocase_profiles.vectors(profile)):
        s = Inputs(c, k, form)
        s.operand(('Ra',), a, 4); s.operand(('Sb', 'Rb', 'URb'), b, 6)
        if op != 'VISET':
            s.predicate(('Pp',), pred, 0)
            effective = pred ^ (form.get('Pp@not') == 'set')
            if op in ('VIADDMNMX', 'VIMNMX3'): s.operand(('Sc', 'Rc', 'URc'), acc, 8)
            if op == 'VIADDMNMX':
                result = integer.viaddmnmx(a, b, acc, width, signed, effective, form['relu'] == 'RELU',
                                          negate_b=form.get(srcb + '@negate') == 'set')
            else:
                inputs = (a, b, acc) if op == 'VIMNMX3' else (a, b)
                result = integer.vminmax(inputs, width, signed, effective, form['relu'] == 'RELU')
        else: result = integer.vset_mask(a, b, width, signed, form['icmp'])
        s.add({s.output(('Rd',), destinations[j % len(destinations)]):result}, form)
    return c


def integer_cases(c, k, form, profiles):
    op = k.mnemonic
    p = source(k, 'Pp', 'UPp'); low = source(k, 'Pr', 'UPr')
    allowed = ({'icmp', 'fmt', 'bop', 'ex', p + '@not', low + '@not' if low else ''}
               if op in ('ISETP', 'UISETP') else
               {'fmt', p + '@not'} if op == 'IMNMX' else {p + '@not'} if op in ('SEL', 'USEL') else
               {'mode', 'SrcAFmt', 'SrcBFmt', 'Rc@negate'} if op == 'IDP' else {'dir', 'cw', 'fmt', 'hilo', 'xor'})
    if set(form) - allowed: raise Unsupported('unhandled modifiers: ' + ', '.join(sorted(set(form) - allowed)))
    if op in ('ISETP', 'UISETP', 'IMNMX') and form.get('fmt') not in ('U32', 'S32'):
        raise Unsupported('unsupported integer format')
    if op in ('ISETP', 'UISETP'):
        if form.get('icmp') not in integer.COMPARISONS or form.get('bop') not in ('AND', 'OR', 'XOR'):
            raise Unsupported('unsupported integer comparison/combiner')
        if form.get('ex', 'noex') not in ('EX', 'noex'): raise Unsupported('unknown extended comparison mode')
    if op in ('SHF', 'USHF') and form.get('xor', 'noxor') != 'noxor':
        raise Unsupported('XOR funnel shifts need an independent reference')
    if op == 'IDP':
        afmts = ('S8', 'U8') if form.get('mode') == '4A' else ('S16', 'U16')
        if form.get('mode') not in ('4A', '2A.LO', '2A.HI') or form.get('SrcAFmt') not in afmts or form.get('SrcBFmt') not in ('S8', 'U8'):
            raise Unsupported('unsupported dot-product format')
    for j, (a, b, acc, pred, prior) in enumerate(vector for profile in profiles for vector in autocase_profiles.vectors(profile)):
        s = Inputs(c, k, form)
        s.operand(('Ra', 'URa'), a, 4)
        s.operand(('Sb', 'Rb', 'URb'), b, 6)
        if op in ('ISETP', 'UISETP', 'IMNMX', 'SEL', 'USEL'):
            s.predicate((p,), pred, 0 if j & 1 else 4)
            effective = pred ^ (form.get(p + '@not') == 'set')
        if op in ('ISETP', 'UISETP'):
            if low: s.predicate((low,), prior, 1 if j & 1 else 5)
            inherited = prior ^ (form.get(low + '@not') == 'set') if low and form.get('ex') == 'EX' else None
            pu, pv = integer.isetp(a, b, form['icmp'], form['bop'], effective, form['fmt'] == 'S32', inherited)
            expected = {s.output(('Pu', 'UPu'), 0):pu, s.output(('Pv', 'UPv'), 1):pv}
        else:
            if op == 'IMNMX': result = integer.vminmax((a, b), 32, form['fmt'] == 'S32', effective)
            elif op in ('SEL', 'USEL'): result = a if effective else b
            else:
                s.operand(('Sc', 'Rc', 'URc'), acc, 8)
                if op == 'IDP':
                    acc = -acc & 0xffffffff if form.get('Rc@negate') == 'set' else acc
                    if form['mode'] == '4A':
                        result = integer.dot4(a, b, acc, form['SrcAFmt'] == 'S8', form['SrcBFmt'] == 'S8')
                    else:
                        result = integer.dot2(a, b, acc, form['SrcAFmt'] == 'S16', form['SrcBFmt'] == 'S8', form['mode'] == '2A.HI')
                else: result = integer.shf(a, b, acc, form['fmt'], form['dir'], form['cw'], form['hilo'])
            expected = {s.output(('Rd', 'URd'), 4 if j & 1 else 2): result}
        s.add(expected, form)
    return c


def saved_plan(arch):
    path = PLANS / (arch + '.json')
    if not path.exists(): raise ValueError(f'{path}: missing generated coverage plan; run python -m tests.autocases {arch} --write')
    data = json.loads(path.read_text())
    if data['version'] != 2: raise ValueError(f'{path}: unsupported shared-case plan version; regenerate it')
    if data['arch'] != arch: raise ValueError(f'{path}: architecture does not match its filename')
    return data


def stream(arch, fp, family, start=0, step=1, sample=sampling.DEFAULT):
    """The family's generated cases, form by form; with start/step, every step-th planned form from start."""
    if family not in FAMILIES.values(): return
    entries = [e for e in saved_plan(arch)['forms'] if e['family'] == family][start::step]   # not the whole plan (150 MiB for SM89)
    for entry in entries:
        yield from generate(arch, entry['cls'], entry['settings'], fp, entry['profiles'], entry['native'], sample=sample)


def append(c, family, sample=sampling.DEFAULT): c.extend(stream(c.arch, c.fp, family, sample=sample))
