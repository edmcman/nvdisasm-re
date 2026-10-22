"""Source directives, kernel declarations, symbols, and two-pass label resolution."""
import json
import re
from dataclasses import dataclass, field
from functools import cache

from sass.assemble import encoding, instruction
from sass.assemble.instruction import AssemblyError
from sass.decode import ir
from sass.md import expression
from sass.paths import MD


@dataclass(frozen=True)
class Statement:
    text: str
    line: int
    column: int


@dataclass
class Parameter:
    name: str
    size: int
    alignment: int
    offset: int


@dataclass
class Kernel:
    name: str
    statements: list = field(default_factory=list)
    parameters: list = field(default_factory=list)
    registers: int | None = None
    uniform_registers: int = 0
    shared: int = 0
    frame_size: int = 0
    constants: dict = field(default_factory=dict)
    code: bytes = b''
    instructions: list = field(default_factory=list)

    @property
    def parameter_size(self):
        return max((p.offset + p.size for p in self.parameters), default=0)


@dataclass
class Module:
    arch: str
    kernels: list


def align(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def statements(source, filename='<string>'):
    """Comments retain their positions so diagnostics point into the original source."""
    def erase(match):
        return re.sub(r'[^\n]', ' ', match[0])
    source = re.sub(r'/\*.*?\*/|//[^\n]*', erase, source, flags=re.S)
    if '/*' in source:
        raise AssemblyError('unterminated block comment', filename)
    result, pos = [], 0
    while pos < len(source):
        if source[pos].isspace() or source[pos] == ';':
            pos += 1
            continue
        start = pos
        line = source.count('\n', 0, start) + 1
        column = start - source.rfind('\n', 0, start)
        if m := re.match(r'[A-Za-z_.$][\w.$]*\s*:', source[pos:]):
            pos += m.end()
        elif source[pos] == '.':
            end = re.search(r'[;\n]', source[pos:])
            pos = len(source) if end is None else pos + end.start()
        else:
            end = source.find(';', pos)
            if end < 0:
                raise AssemblyError('instruction must end with a semicolon', filename, line, column)
            pos = end + 1
        result.append(Statement(source[start:pos].strip().removesuffix(';').strip(), line, column))
    return result


def target(text):
    arch = text.strip().upper().replace('SM_', 'SM')
    if not re.fullmatch(r'SM\d+', arch) or not (MD / arch / 'md').is_dir():
        raise ValueError(f'unknown machine-description target {text!r}')
    return arch


def error(exc, statement, filename):
    return AssemblyError(str(exc), filename, statement.line, statement.column)


def operands(text):
    return [part.strip() for part in text.split(',')]


def integer(text, minimum=0, maximum=0xffffffff):
    value = encoding.literal(text)
    if not minimum <= value <= maximum:
        raise ValueError(f'{text}: expected a value from {minimum} to {maximum}')
    return value


def module(source, arch=None, filename='<string>'):
    try:
        selected = target(arch) if arch is not None else None
    except ValueError as exc:
        raise AssemblyError(str(exc), filename) from None
    kernels, current = [], None
    for statement in statements(source, filename):
        text = statement.text
        try:
            if text.startswith('.target '):
                wanted = target(text.split(None, 1)[1])
                if selected is not None and selected != wanted:
                    raise ValueError(f'conflicting targets {selected} and {wanted}')
                selected = wanted
            elif text.startswith('.kernel '):
                if current is not None:
                    raise ValueError('nested .kernel declaration')
                name = text.split(None, 1)[1].strip()
                if not re.fullmatch(r'[A-Za-z_]\w*', name):
                    raise ValueError('invalid kernel name')
                if any(k.name == name for k in kernels):
                    raise ValueError(f'duplicate kernel {name}')
                current = Kernel(name)
                kernels.append(current)
            elif text == '.endkernel':
                if current is None:
                    raise ValueError('.endkernel outside a kernel')
                current = None
            elif current is None:
                raise ValueError('expected .target or .kernel')
            elif text.startswith('.param '):
                args = operands(text.split(None, 1)[1])
                if len(args) != 3 or not re.fullmatch(r'[A-Za-z_]\w*', args[0]):
                    raise ValueError('expected .param NAME, SIZE, ALIGNMENT')
                name, size, alignment = args[0], integer(args[1], 1, 4096), integer(args[2], 1, 16)
                if alignment & (alignment - 1):
                    raise ValueError('parameter alignment must be a power of two')
                if any(p.name == name for p in current.parameters):
                    raise ValueError(f'duplicate parameter {name}')
                offset = align(current.parameter_size, alignment)
                if offset + size > 4096:
                    raise ValueError('parameter bank exceeds the supported 4096-byte limit')
                current.parameters.append(Parameter(name, size, alignment, offset))
            elif text.startswith('.registers '):
                args = operands(text.split(None, 1)[1])
                if len(args) not in (1, 2) or current.registers is not None:
                    raise ValueError('expected one .registers GPR_COUNT[, UNIFORM_COUNT] declaration')
                current.registers = integer(args[0], 1, 255)
                current.uniform_registers = integer(args[1], 0, 63) if len(args) == 2 else 0
            elif text.startswith('.shared '):
                current.shared = integer(text.split(None, 1)[1], 0, 0xffffff)
            elif text.startswith('.constant '):
                args = operands(text.split(None, 1)[1])
                if len(args) != 2:
                    raise ValueError('expected .constant BANK, "HEX_BYTES"')
                bank = integer(args[0], 1, 17)
                if bank in current.constants:
                    raise ValueError(f'duplicate constant bank {bank}')
                if not re.fullmatch(r'"[\da-fA-F\s]*"', args[1]):
                    raise ValueError('constant contents must be quoted hexadecimal bytes')
                body = bytes.fromhex(args[1][1:-1])
                if len(body) > 65536:
                    raise ValueError('constant bank exceeds 64 KiB')
                current.constants[bank] = body
            else:
                current.statements.append(statement)
        except ValueError as exc:
            raise error(exc, statement, filename) from None
    if current is not None:
        raise AssemblyError(f'missing .endkernel for {current.name}', filename)
    if selected is None:
        raise AssemblyError('target required: use .target SMxx or --arch', filename)
    if not kernels:
        raise AssemblyError('at least one .kernel is required', filename)
    return Module(selected, kernels)


def symbol_values(kernel, parameter_base):
    return {p.name: parameter_base + p.offset for p in kernel.parameters}


def sequence(arch, items, base=0, symbols=None, filename='<string>'):
    if base < 0 or base % 16:
        raise AssemblyError('base address must be a nonnegative multiple of 16', filename)
    labels, offset = {}, base
    for statement in items:
        text = statement.text
        if text.endswith(':'):
            label = text[:-1].strip()
            if label in labels or label in (symbols or {}):
                raise AssemblyError(f'duplicate symbol {label}', filename, statement.line, statement.column)
            labels[label] = offset
        elif not text.startswith('.'):
            offset += 16
    bindings = (symbols or {}) | labels
    output, records, schedule = bytearray(), [], {}
    for statement in items:
        text = statement.text
        if text.endswith(':'):
            continue
        try:
            if text.startswith('.schedule '):
                schedule = instruction.parse_schedule(text.split(None, 1)[1])
                continue
            if text == '.schedule':
                schedule = {}
                continue
            if text.startswith('.'):
                raise ValueError(f'unknown directive {text.split()[0]}')
            def replace(match):
                name = match[1]
                if name not in bindings:
                    raise ValueError(f'undefined symbol {name}')
                value = bindings[name]
                if match.lastindex and match.lastindex >= 3 and match[2]:
                    addend = integer(match[3])
                    value += addend if match[2] == '+' else -addend
                return hex(value)
            text = re.sub(r'\$([A-Za-z_][\w.]*)(?:\s*([+-])\s*(0[xX][\da-fA-F]+|\d+))?', replace, text)
            # nvdisasm symbolic targets use `(label); accept those and bare known labels.
            text = re.sub(r'`\(([\w.$]+)\)', lambda m: replace(m), text)
            data, k, env, widths = instruction.assemble(arch, text, base + len(output), schedule, labels)
            records.append((k, env, widths, statement))
            output.extend(data)
        except ValueError as exc:
            raise error(exc, statement, filename) from None
    return bytes(output), records


def raw(arch, source, base=0, filename='<string>'):
    try:
        selected = target(arch) if arch is not None else None
    except ValueError as exc:
        raise AssemblyError(str(exc), filename) from None
    items = []
    for statement in statements(source, filename):
        if statement.text.startswith('.target '):
            try:
                wanted = target(statement.text.split(None, 1)[1])
            except ValueError as exc:
                raise error(exc, statement, filename) from None
            if selected is not None and selected != wanted:
                raise error(ValueError(f'conflicting targets {selected} and {wanted}'), statement, filename)
            selected = wanted
        else:
            items.append(statement)
    if selected is None:
        raise AssemblyError('raw assembly requires a target', filename)
    return sequence(selected, items, base, filename=filename)[0]


@cache
def roles(arch):
    return {c['cls']: c for p in sorted((MD / arch / 'semantics').glob('*.json'))
            for c in json.loads(p.read_text())}


def validate_resources(arch, kernel, filename='<string>'):
    if kernel.registers is None:
        raise AssemblyError(f'{kernel.name}: .registers is required', filename)
    md = ir.load(arch)
    for k, env, widths, statement in kernel.instructions:
        for operand in roles(arch)[k.name]['operands']:
            resource = operand['resource']
            if resource not in ('GPR', 'UGPR'):
                continue
            name = operand['name']; value = env.get(name)
            if value is None:
                continue
            atom = k.operand_types.get(name)
            if atom is not None:
                zero = md.enums.get(atom.type, {}).get('RZ' if resource == 'GPR' else 'URZ')
                if value == zero:
                    continue
            span = operand.get('span')
            if span is None:
                span = 1
            if isinstance(span, str):
                span = expression.evaluate(span, md, env)
            limit = kernel.registers if resource == 'GPR' else kernel.uniform_registers
            if value + span > limit:
                raise error(ValueError(f'{kernel.name}: {name} uses registers {value}..{value + span - 1}, '
                                       f'exceeding declared {resource} count {limit}'), statement, filename)
