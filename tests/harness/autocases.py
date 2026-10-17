"""Reference adapters for generated shared-case form plans. No implementation-derived outputs."""
import json
from sass.paths import ROOT
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness.case import Cases, put, source, cx_source
from tests.refs import integer
from tests.harness import autocase_profiles

PLANS = ROOT / 'tests/cases/generated'
OPS = {'BREV', 'POPC', 'FLO', 'SGXT', 'PRMT', 'UPRMT', 'IABS', 'MOV', 'UMOV', 'BMSK'}
INTEGER_OPS = {'ISETP', 'UISETP', 'IMNMX', 'SEL', 'USEL', 'IDP', 'SHF', 'USHF'}
PACKED_OPS = {'VIMNMX', 'VISET', 'VIADDMNMX', 'VIMNMX3'}
INTEGER_OPS |= PACKED_OPS
FAMILIES = {op: 'int_simd' if op in PACKED_OPS | {'IDP'} else 'bmsk' if op == 'BMSK' else 'int_alu' for op in OPS | INTEGER_OPS}
OPS |= INTEGER_OPS
OWNED = OPS - {'FLO'}  # FLO's partially modelled predicate outputs retain explicit PT-only cases.


class Unsupported(ValueError): pass


def generate(arch, cls, form, fp='exact', profiles=None, native=True):
    """Build a full form atomically; reject unfamiliar operands/modifiers rather than guess."""
    c = Cases(arch, fp, exhaustive=False)
    if cls not in c.classes: raise Unsupported('alternate or absent instruction class')
    k = c.classes[cls]
    if k.mnemonic not in OPS:
        raise Unsupported('needs a reference/operand adapter')
    profiles = autocase_profiles.names(k.mnemonic, cls, form) if profiles is None else profiles
    if not profiles or any(name not in autocase_profiles.PROFILES for name in profiles):
        raise Unsupported('empty or unknown input profile')
    if k.mnemonic in ('IABS', 'MOV', 'UMOV', 'BMSK'):
        return finish(scalar_cases(c, k, form, profiles), native)
    if k.mnemonic in PACKED_OPS: return finish(packed_cases(c, k, form, profiles), native)
    if k.mnemonic in INTEGER_OPS: return finish(integer_cases(c, k, form, profiles), native)
    profile = 'permutation' if k.mnemonic in ('PRMT', 'UPRMT') else 'sgxt' if k.mnemonic == 'SGXT' else 'bits'
    if profiles != [profile]: raise Unsupported('incompatible input profiles')
    src = source(k, 'Sb') if 'Sb' in k.operand_types else source(k, 'Rb', 'URb')
    dest = source(k, 'Rd', 'URd')
    if src is None or dest is None:
        raise Unsupported('unsupported operand layout')
    allowed = {'fmt', 'sh'} if k.mnemonic == 'FLO' else {'fmt', 'cw'} if k.mnemonic == 'SGXT' else {'pmode', 'idx'} if k.mnemonic in ('PRMT', 'UPRMT') else set()
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
            for width in (spec['widths'] if k.mnemonic == 'SGXT' else (None,)):
                banks = spec.get('banks', ((2, 16),)) if k.operand_types[src].type == 'C' else ((2, 16),)
                for bank in banks:
                    s = Inputs(c, k, form, bank=bank)
                    operand = value if width is None else width
                    s.operand((src,), operand, 6)
                    operand = (~operand & 0xffffffff) if form.get(src + '@invert') == 'set' else operand
                    if k.mnemonic == 'BREV': result = integer.brev(operand)
                    elif k.mnemonic == 'POPC': result = operand.bit_count()
                    elif k.mnemonic == 'FLO':
                        if form.get('sh', 'nosh') not in ('SH', 'nosh'): raise Unsupported('unknown FLO mode')
                        s.fields['Pu'] = 'PT'
                        result = integer.flo(operand, form.get('sh') == 'SH', form.get('fmt') == 'S32')
                    else:
                        if form.get('cw', 'C') not in ('C', 'W'): raise Unsupported('unknown SGXT mode')
                        s.operand(('Ra',), value, 4)
                        result = integer.sgxt(value, operand, form.get('cw') == 'W', form.get('fmt') == 'S32')
                    s.add({s.output((dest,), 2):result}, form)
    return finish(c, native)


def finish(c, native):
    """Union overlapping profiles, rejecting inconsistent expectations for the same scenario."""
    if not c: raise Unsupported("input profiles produced no cases")
    unique = {}
    for case in c:
        if native:
            case = case._replace(expected={**case.expected, '__native':True}, request={**case.request, 'inspect_pcode':True})
        if case.id in unique and unique[case.id] != case: raise ValueError(f'{case.id}: conflicting generated expectations')
        unique[case.id] = case
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

    def add(self, expected, form):
        word = encoding.encode(self.c.arch, self.k.name, **self.fields)
        decoded = pydecode.decode(self.c.arch, bytes.fromhex(word))
        if decoded.klass.name != self.k.name or settings(self.c.arch, decoded) != form:
            raise Unsupported('encoded instruction does not preserve the requested class/form')
        self.c.add(self.k.name, self.fields, self.registers, expected, memory=self.memory)


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
                b = -b & 0xffffffff if form.get(srcb + '@negate') == 'set' else b
                result = integer.viaddmnmx(a, b, acc, width, signed, effective, form['relu'] == 'RELU')
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


def append(c, family):
    if family not in FAMILIES.values(): return
    path = PLANS / (c.arch + '.json')
    if not path.exists(): raise ValueError(f'{path}: missing generated coverage plan; run python -m tests.autocases {c.arch} --write')
    plan = json.loads(path.read_text())
    if plan['version'] != 2: raise ValueError(f'{path}: unsupported shared-case plan version; regenerate it')
    if plan['arch'] != c.arch: raise ValueError(f'{path}: architecture does not match its filename')
    for entry in plan['forms']:
        if entry['family'] == family:
            c.extend(generate(c.arch, entry['cls'], entry['settings'], c.fp, entry['profiles'], entry['native']))
