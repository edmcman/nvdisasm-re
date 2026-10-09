"""Conservative md-based lowering identity, independent of compiler IR pointers."""
import hashlib
import json
from functools import cache
from pathlib import Path
from sass import cubin, ir, mdexpr, pydecode
from sass.gen_sleigh import ENUM_FILE
import mdlib

ARCHITECTURES = ('SM75','SM80','SM86','SM89','SM90','SM100','SM101','SM103','SM120')
TARGETS = {a: 'sm_' + ('110' if a == 'SM101' else a[2:]) for a in ARCHITECTURES}
# Only established arithmetic data operands are abstracted. Shift counts, LUTs,
# address literals and unknown conventions stay literal.
# LOP3's Sb is a data immediate; its truth table (imm8) is not in DATA_NAMES and stays literal.
DATA_OPS = {'MOV','UMOV','MOV64I','MOV64IUR','IADD','IADD3','UIADD3','IMAD','UIMAD','LOP3','ULOP3',
            'FADD','FMUL','FFMA','DADD','DMUL','DFMA','HADD2','HFMA2','FSEL','FMNMX'}
DATA_NAMES = {'Sa','Sb','Sc','sImm','uImm','fImm','dImm','imm','imm32','imm64',
              'fImmH0','fImmH1','SbH0','SbH1','ScH0','ScH1'}

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

@cache
def roles(arch):
    return {r['cls']: r for p in (mdlib.OUT / arch / 'semantics').glob('*.json')
            for r in json.loads(p.read_text())}

class Registers:
    """Canonical cells preserve partial overlap as well as exact aliasing."""
    def __init__(self): self.cells = {}; self.next = {}
    def view(self, bank, index, span, zero):
        if index == zero: return ['constant', bank, span, 'true' if bank in ('P','UP') else 'zero']
        result = []
        for i in range(index, index + span):
            cell = bank, i
            if cell not in self.cells:
                self.cells[cell] = self.next.get(bank, 0)
                self.next[bank] = self.cells[cell] + 1
            result.append(self.cells[cell])
        return ['register', bank, result]

def atom_value(a, arch, env):
    if a.name in env: return env[a.name]
    if a.type in arch.enums:
        enum = arch.enums[a.type]
        if a.default is not None: return enum.get(a.default, a.default)
        if len(set(enum.values())) == 1: return next(iter(enum.values()))
    return None

def branch_target(d, arch):
    _, widths = pydecode.decode_env(arch, d.klass, int.from_bytes(d.word, 'little'))
    for a in d.klass.format:
        if a.type == 'RSImm':
            return d.env['__addr'] + 16 + pydecode.sext(d.env[a.name], widths.get(a.name, 32))
    return None

def signature(d, arch, registers=None, labels=None):
    k, env = d.klass, d.env
    registers = registers or Registers()
    record = roles(arch.name).get(k.name, {})
    by_name = {o['name']: o for o in record.get('operands', [])}
    _, widths = pydecode.decode_env(arch, k, int.from_bytes(d.word, 'little'))
    operands = []
    for a in k.format:
        if a.kind not in ('operand','guard','mod') or a.type == 'REUSE': continue
        v = atom_value(a, arch, env)
        item = dict(name=a.name, type=env.get(('__type',a.name), a.type), kind=a.kind)
        role = by_name.get(a.name, {})
        item['roles'] = role.get('role') or []
        if a.type in ENUM_FILE:
            bank = ENUM_FILE[a.type]; span = role.get('span') or 1
            if isinstance(span, str): span = int(mdexpr.evaluate(span, arch, env))
            item['bits'] = span * (1 if bank in ('P','UP') else 32)
            zero = {'R':255,'P':7,'UP':7}.get(bank, arch.enums['UniformRegister']['URZ'])
            if bank == 'UR' and a.name in widths: zero &= (1 << widths[a.name]) - 1
            item['value'] = registers.view(bank, v, span, zero) if isinstance(v,int) else ['unknown',v]
        elif a.type == 'RSImm':
            target = branch_target(d, arch)
            item['value'] = ['label', labels.get(target, 'external') if labels is not None else 'target']
        elif pydecode.IMM.match(a.type):
            item['bits'] = widths.get(a.name, 32)
            if k.mnemonic in DATA_OPS and a.name in DATA_NAMES:
                item['value'] = ['parameter', env.get(('__fmt',a.name), a.type), item['bits']]
            else: item['value'] = ['literal',v]
        else: item['value'] = v
        item['attributes'] = {key[1]: value for key,value in env.items()
                              if isinstance(key,tuple) and key[0] == a.name}
        operands.append(item)
    # Explicit compound operand type captures cbank/shared/local/descriptor form.
    return dict(cls=k.name, opcode=k.mnemonic, operands=operands,
                syntax=[(a.kind,a.type,a.name) for a in k.format
                        if a.kind in ('open','close','plus','flag','word')])

def decode_section(architecture, body, section='.text'):
    arch = ir.load(architecture); forms = {}; listing = []; errors = []; decoded = {}
    for index, word in enumerate(cubin.words(body)):
        addr = index * 16
        try:
            d = pydecode.decode(architecture, word, addr); d.word = word
            decoded[addr] = d
            listing.append(f'{section}+{addr:04x}: {d.text};')
        except pydecode.NoMatch as e:
            errors.append(f'{section}+{addr:04x}: {e}')
    # Reachability eliminates branch-to-self alignment tails and dead padding.
    pending = [0]; live = set()
    while pending:
        addr = pending.pop()
        if addr in live or addr not in decoded: continue
        live.add(addr); d = decoded[addr]
        guard = next((a for a in d.klass.format if a.kind == 'guard'), None)
        unconditional = guard is None or (d.env.get(guard.name,7) == 7 and not d.env.get((guard.name,'not'),0))
        target = branch_target(d, arch)
        if target is not None: pending.append(target)
        # BRA has additional predicates/modes: suppress fallthrough only for
        # plain unconditional forms. Conservative for unfamiliar selectors.
        plain_bra = d.klass.mnemonic == 'BRA' and d.env.get('Pp',7) == 7
        if not (unconditional and (d.klass.mnemonic in ('EXIT','RET') or plain_bra)):
            pending.append(addr + 16)
    labels = {addr:i for i,addr in enumerate(sorted(live))}; registers = Registers(); sequence = []
    for addr in sorted(live):
        d = decoded[addr]
        form = signature(d, arch); forms[digest(form)] = form
        sequence.append(signature(d, arch, registers, labels))
    return forms, sequence, listing, errors

def decode_cubin(data, architecture):
    encoded = cubin.elf_arch(data)
    expected = 'SM110' if architecture == 'SM101' else architecture
    if encoded != expected: raise ValueError(f'target mismatch: requested {architecture}, ELF {encoded}, expected {expected}')
    forms = {}; sequences = []; listing = []; errors = []
    for section, body in cubin.text_sections(data).items():
        f, sequence, l, e = decode_section(architecture, body, section)
        forms.update(f); listing += l; errors += e
        if sequence: sequences.append(sequence)
    if errors: return dict(status='undecodable', diagnostics=errors, listing=listing, forms={}, sequences=[])
    if not sequences: raise ValueError('no executable kernel sections')
    return dict(status='decoded', elf_arch=encoded, forms=forms, sequences=sequences, listing=listing, diagnostics=[])
