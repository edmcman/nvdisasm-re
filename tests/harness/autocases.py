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


def append(c, family):
    path = PLANS / (c.arch + '.json')
    if not path.exists(): return
    plan = json.loads(path.read_text())
    if plan['version'] != 1: raise ValueError(f'{path}: unsupported shared-case plan version')
    for entry in plan['forms']:
        if entry['family'] == family:
            c.extend(generate(c.arch, entry['cls'], entry['settings'], c.fp))
