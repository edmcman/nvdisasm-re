"""Reference adapters for generated shared-case form plans. No implementation-derived outputs."""
import json
from sass.paths import ROOT
from sass.assemble import encoding
from sass.decode import pydecode
from sass.sleigh.modifiers import settings
from tests.harness.case import Cases, put, source, is_cx, cx_source
from tests.refs import integer

PLANS = ROOT / 'tests/cases/generated'
OPS = {'BREV', 'POPC', 'FLO', 'SGXT', 'PRMT', 'UPRMT'}
INTEGER_OPS = {'ISETP', 'UISETP', 'IMNMX', 'SEL', 'USEL', 'IDP', 'SHF', 'USHF'}
FAMILIES = {op: 'int_simd' if op == 'IDP' else 'int_alu' for op in OPS | INTEGER_OPS}
OPS |= INTEGER_OPS
VALUES = (0, 1, 0x80000000, 0xffffffff, 0x80ff017f, 0x55555555)


class Unsupported(ValueError): pass


def generate(arch, cls, form, fp='exact'):
    """Build a full form atomically; reject unfamiliar operands/modifiers rather than guess."""
    c = Cases(arch, fp, exhaustive=False)
    if cls not in c.classes: raise Unsupported('alternate or absent instruction class')
    k = c.classes[cls]
    if k.mnemonic not in OPS:
        raise Unsupported('needs a reference/operand adapter')
    if is_cx(k) and arch not in ('SM75', 'SM80', 'SM86', 'SM89', 'SM90'):
        raise Unsupported('CX descriptor setup not established on this architecture')
    if k.mnemonic in INTEGER_OPS: return integer_cases(c, k, form)
    src = source(k, 'Sb') if 'Sb' in k.operand_types else source(k, 'Rb', 'URb')
    dest = source(k, 'Rd', 'URd')
    if src is None or dest is None:
        raise Unsupported('unsupported operand layout')
    allowed = {'fmt', 'sh'} if k.mnemonic == 'FLO' else {'fmt', 'cw'} if k.mnemonic == 'SGXT' else {'pmode'} if k.mnemonic in ('PRMT', 'UPRMT') else set()
    allowed |= {src + '@invert'}
    if set(form) - allowed:
        raise Unsupported('unhandled modifiers: ' + ', '.join(sorted(set(form) - allowed)))
    if form.get('fmt', 'U32') not in ('S32', 'U32'):
        raise Unsupported('unsupported integer format')
    fields = {n: int(v == 'set') if '@' in n else v for n, v in form.items()}
    for value in VALUES:
        for width in ((0, 1, 31, 32, 33, 0xffffffff) if k.mnemonic == 'SGXT' else (None,)):
            f, regs, memory = dict(fields, **{dest:2}), {}, []
            def place(name, bits, reg):
                if k.operand_types[name].type == 'CX':
                    fs, rs, ms = cx_source(arch, k, name, bits, ur=10)
                    f.update(fs); regs.update(rs); memory.extend(ms)
                elif not put(k, name, bits, f, regs, memory, reg, bank=(2, reg * 4), imm=('SImm', 'UImm')):
                    raise Unsupported('unsupported source type')
            operand = value if width is None else width
            place(src, operand, 6)
            operand = (~operand & 0xffffffff) if form.get(src + '@invert') == 'set' else operand
            if k.mnemonic == 'BREV': result = integer.brev(operand)
            elif k.mnemonic == 'POPC': result = operand.bit_count()
            elif k.mnemonic == 'FLO':
                if form.get('sh', 'nosh') not in ('SH', 'nosh'): raise Unsupported('unknown FLO mode')
                result = integer.flo(operand, form.get('sh') == 'SH', form.get('fmt') == 'S32')
            elif k.mnemonic == 'SGXT':
                if form.get('cw', 'C') not in ('C', 'W'): raise Unsupported('unknown SGXT mode')
                f['Ra'] = 4; regs['R4'] = value
                result = integer.sgxt(value, operand, form.get('cw') == 'W', form.get('fmt') == 'S32')
            else:
                mode = form.get('pmode', 'IDX')
                if mode not in ('IDX', 'F4E', 'B4E', 'RC8', 'RC16', 'ECL', 'ECR'):
                    raise Unsupported('unknown PRMT mode')
                left, right = source(k, 'Ra', 'URa'), source(k, 'Sc', 'Rc', 'URc')
                if left is None or right is None: raise Unsupported('unsupported PRMT layout')
                place(left, 0x80ff017f, 4); place(right, 0x55aa0081, 8)
                result = integer.prmt(0x80ff017f, operand, 0x55aa0081, mode)
            # Strict encoding checks operand-dependent legality as well as the form table.
            word = encoding.encode(arch, cls, **f)
            d = pydecode.decode(arch, bytes.fromhex(word))
            if d.klass.name != cls or settings(arch, d) != form:
                raise Unsupported('encoded instruction does not preserve the requested class/form')
            c.add(cls, f, regs, {('UR2' if dest == 'URd' else 'R2'): result}, memory=memory)
    return c


class Inputs:
    """Place logical operands without confusing CX descriptor children with data operands."""
    def __init__(self, c, k, form):
        self.c, self.k = c, k
        self.fields = {n: int(v == 'set') if '@' in n else v for n, v in form.items()}
        self.registers, self.memory = {}, []

    def operand(self, names, value, reg):
        name = source(self.k, *names)
        if name is None: raise Unsupported('unsupported operand layout: ' + '/'.join(names))
        kind = self.k.operand_types[name].type
        if kind == 'CX':
            fs, rs, ms = cx_source(self.c.arch, self.k, name, value, ur=10)
            self.fields.update(fs); self.registers.update(rs); self.memory.extend(ms)
        else:
            # Signed immediates encode the signed interpretation of the same input bits.
            value = integer.sext(value, 32) if kind == 'SImm' else value
            if not put(self.k, name, value, self.fields, self.registers, self.memory, reg,
                       bank=(2, reg * 4), imm=('SImm', 'UImm')):
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


# Equal words exercise every predicate/low-result pair; remaining vectors distinguish
# signed comparison, lane signedness, and nonzero accumulators. Outputs also alias inputs.
VECTORS = ((0, 0, 0, 0, 0), (0, 0, 1, 0, 1), (0, 0, 0xffffffff, 1, 0), (0, 0, 0x80000000, 1, 1),
           (0x80000000, 0x7fffffff, 0xffffffff, 0, 1), (0x7fffffff, 0x80000000, 1, 1, 0),
           (0xffffffff, 0, 0x12345678, 1, 1), (0x807f01ff, 0x7f80ff00, 0x10203040, 0, 0))


def integer_cases(c, k, form):
    op = k.mnemonic
    p = source(k, 'Pp', 'UPp'); low = source(k, 'Pr', 'UPr')
    allowed = ({'icmp', 'fmt', 'bop', 'ex', p + '@not', low + '@not' if low else ''}
               if op in ('ISETP', 'UISETP') else
               {'fmt', p + '@not'} if op == 'IMNMX' else {p + '@not'} if op in ('SEL', 'USEL') else
               {'mode', 'SrcAFmt', 'SrcBFmt', 'Rc@negate'} if op == 'IDP' else {'dir', 'cw', 'fmt', 'hilo'})
    if set(form) - allowed: raise Unsupported('unhandled modifiers: ' + ', '.join(sorted(set(form) - allowed)))
    if op in ('ISETP', 'UISETP', 'IMNMX') and form.get('fmt') not in ('U32', 'S32'):
        raise Unsupported('unsupported integer format')
    if op in ('ISETP', 'UISETP'):
        if form.get('icmp') not in integer.COMPARISONS or form.get('bop') not in ('AND', 'OR', 'XOR'):
            raise Unsupported('unsupported integer comparison/combiner')
        if form.get('ex', 'noex') not in ('EX', 'noex'): raise Unsupported('unknown extended comparison mode')
    if op == 'IDP':
        afmts = ('S8', 'U8') if form.get('mode') == '4A' else ('S16', 'U16')
        if form.get('mode') not in ('4A', '2A.LO', '2A.HI') or form.get('SrcAFmt') not in afmts or form.get('SrcBFmt') not in ('S8', 'U8'):
            raise Unsupported('unsupported dot-product format')
    for j, (a, b, acc, pred, prior) in enumerate(VECTORS):
        s = Inputs(c, k, form)
        s.operand(('Ra', 'URa'), a, 4)
        # Shift counts exercise wrap/clamp boundaries for both 32- and 64-bit modes.
        b = (0, 1, 31, 32, 33, 63, 64, 65)[j] if op in ('SHF', 'USHF') else b
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
    path = PLANS / (c.arch + '.json')
    if not path.exists(): return
    plan = json.loads(path.read_text())
    if plan['version'] != 1: raise ValueError(f'{path}: unsupported shared-case plan version')
    for entry in plan['forms']:
        if entry['family'] == family:
            c.extend(generate(c.arch, entry['cls'], entry['settings'], c.fp))
