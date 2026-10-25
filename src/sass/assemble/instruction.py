"""Invert the decoder's FORMAT renderer to parse SASS without an opcode grammar list."""
import itertools
import math
import re
import struct
from fractions import Fraction
from functools import cache

from sass.assemble import encoding
from sass.decode import ir, pydecode
from sass.sleigh.gen import default_value


NUMBER = r'[+-]?(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|INF|QNAN|SNAN)'
MARKER = re.compile(r'«(\d+)»')


class AssemblyError(ValueError):
    """An assembler diagnostic with a source location."""

    def __init__(self, message, filename='<string>', line=1, column=1):
        self.message, self.filename, self.line, self.column = message, filename, line, column
        super().__init__(f'{filename}:{line}:{column}: {message}')


def normalize(text):
    return re.sub(r'\s+', '', text).upper()


class TemplateView:
    def __init__(self, desc):
        self.atoms = []
        self.desc = desc

    def value(self, a, item):
        self.atoms.append(a)
        return f'«{len(self.atoms) - 1}»', False

    def flag(self, name, attr, item):
        return False

    def raw(self, name, item):
        return self.desc

    def target(self, a, off):
        return off


def flags_of(k):
    pending, out = [], {}
    for a in k.format:
        if a.kind == 'flag':
            pending.append(a.name)
        elif a.kind in ('operand', 'guard'):
            out[a.name] = tuple(pending)
            pending = []
    return out


def optional(md, a):
    return default_value(md, a) is not None


def atom_pattern(md, k, a):
    etype = next((t for n, t, _, _ in k.aliases if n == a.name), a.type)
    if etype in md.enums:
        return '(?:' + '|'.join(re.escape(n) for n in sorted(md.enums[etype], key=len, reverse=True)) + ')'
    if pydecode.IMM.fullmatch(a.type):
        return '(?:' + NUMBER + r'|[A-Za-z_.$][\w.$]*)' if a.type == 'RSImm' else NUMBER
    if a.type == 'BITSET':
        return r'\{\s*(?:\d+(?:\s*,\s*\d+)*)?\s*\}'
    if a.type in pydecode.PREFIX:
        return re.escape(pydecode.PREFIX[a.type])
    # Unknown md operand types must be diagnosed, never guessed.
    raise ValueError(f'unsupported operand type {a.type}')


def compile_item(md, k, text, atoms, flags, groups):
    """Default operands/modifiers are optional; the renderer later verifies exact syntax."""
    # Insert flag spans around an operand and its immediately following suffix modifiers.
    spans = {}
    for m in MARKER.finditer(text):
        i = int(m[1])
        a = atoms[i]
        on = flags.get(a.name, ()) if a.kind in ('operand', 'guard') else ()
        if not on:
            continue
        end = m.end()
        if a.type in ('C', 'CX'):
            # A constant prefix owns both bracketed bank/address operands.
            end = text.rfind(']') + 1
        # At bracket depth zero flags enclose directly following modifiers, excluding REUSE.
        if text[:m.start()].count('[') == text[:m.start()].count(']'):
            while match := re.match(r'\.\s*«(\d+)»', text[end:]):
                if atoms[int(match[1])].type == 'REUSE':
                    break
                end += match.end()
        spans[i] = (m.start(), end, on)
    starts, ends = {}, {}
    for i, (start, end, on) in spans.items():
        prefix, suffix = '', ''
        # Same nesting as pydecode.wrap: absolute innermost, other flags outside it.
        for flag in reversed(sorted(on, key=lambda f: f != 'absolute')):
            if flag == 'absolute':
                prefix += f'(?P<a{i}>\\|?)'
                suffix = f'(?P=a{i})' + suffix
                groups[f'a{i}'] = (atoms[i].name, 'absolute')
            else:
                symbol = {'negate': '-', 'not': '!', 'invert': '~'}[flag]
                prefix += f'(?P<f{i}_{flag}>{re.escape(symbol)})?'
                groups[f'f{i}_{flag}'] = (atoms[i].name, flag)
        starts[start], ends[end] = prefix, suffix
    result, pos = '', 0
    while pos < len(text):
        result += ends.get(pos, '') + starts.get(pos, '')
        if m := MARKER.match(text, pos):
            i = int(m[1]); a = atoms[i]
            token = f'(?P<v{i}>{atom_pattern(md, k, a)})'
            # Dot belongs to its optional modifier, not the previous operand.
            if a.kind == 'mod' and optional(md, a) and result.endswith(r'\s*\.\s*'):
                result = result[:-len(r'\s*\.\s*')] + '(?:\\s*\\.\\s*' + token + ')?'
            else:
                result += '(?:' + token + ')?' if optional(md, a) else token
            pos = m.end()
        else:
            char = text[pos]
            if char.isspace():
                result += r'\s*'
            elif char == '+':
                result += r'\s*\+?\s*'
            elif char in '.[]':
                result += r'\s*' + re.escape(char) + r'\s*'
            else:
                result += re.escape(char)
            pos += 1
    return result + ends.get(pos, '')


@cache
def patterns(arch, name):
    md = ir.load(arch)
    k = next(c for c in md.classes if c.name == name)
    output = []
    flags = flags_of(k)
    for desc in ((0, 1) if any(a.type == 'EXP_DESC' for a in k.format) else (0,)):
        view = TemplateView(desc)
        guard, items = pydecode.render_items(k, view)
        opts = [i for i, text in enumerate(items[1:], 1)
                if (found := MARKER.findall(text)) and all(optional(md, view.atoms[int(n)]) for n in found)]
        for omit in itertools.product((False, True), repeat=len(opts)):
            skipped = {i for i, skip in zip(opts, omit) if skip}
            groups = {}
            parts = [compile_item(md, k, t, view.atoms, flags, groups)
                     for i, t in enumerate(items) if i not in skipped]
            head, *tail = parts
            body = head + (r'\s+' + r'\s*,\s*'.join(tail) if tail else '')
            if guard:
                g = compile_item(md, k, guard.strip(), view.atoms, flags, groups)
                body = '(?:' + g + r'\s+)?' + body
            output.append((re.compile(r'\s*' + body + r'\s*', re.I), tuple(view.atoms), groups, desc))
    return tuple(output)


def float_bits(text, kind):
    """Hex literals specify bits; decimal literals round to the selected format."""
    if '0x' in text.lower() or '0b' in text.lower():
        return encoding.literal(text)
    sign = int(text.startswith('-'))
    ebits, mbits = {'F16': (5, 10), 'F32': (8, 23), 'F64': (11, 52),
                   'E8M7': (8, 7), 'E6M9': (6, 9)}[kind]
    if text.lstrip('+-').upper() in ('INF', 'QNAN', 'SNAN'):
        special = text.lstrip('+-').upper()
        mantissa = 0 if special == 'INF' else 1 << (mbits - 1) if special == 'QNAN' else (1 << 32 if kind == 'F64' else 1)
        bits = sign << (ebits + mbits) | ((1 << ebits) - 1) << mbits | mantissa
    elif kind in ('F16', 'F32', 'F64'):
        fmt = {'F16': ('e', 'H'), 'F32': ('f', 'I'), 'F64': ('d', 'Q')}[kind]
        try:
            value = float(text)
            if not math.isfinite(value):
                raise OverflowError
            bits = struct.unpack('<' + fmt[1], struct.pack('<' + fmt[0], value))[0]
        except (OverflowError, struct.error):
            raise ValueError(f'{text}: not representable as {kind}') from None
    else:
        value = abs(float(text))
        if not math.isfinite(value):
            raise ValueError(f'{text}: not representable as {kind}')
        bias = (1 << (ebits - 1)) - 1
        exponent = max(1, math.frexp(value)[1] - 1 + bias) if value else 1
        shift = bias + mbits - exponent
        scaled = Fraction(value) * (1 << shift) if shift >= 0 else Fraction(value) / (1 << -shift)
        mantissa = round(scaled)
        if mantissa >= 1 << (mbits + 1):
            exponent += 1; mantissa >>= 1
        if exponent >= (1 << ebits) - 1:
            raise ValueError(f'{text}: not representable as {kind}')
        if mantissa < 1 << mbits:
            exponent = 0
        else:
            mantissa -= 1 << mbits
        bits = sign << (ebits + mbits) | exponent << mbits | mantissa
    if kind == 'F64':
        if bits & 0xffffffff:
            raise ValueError(f'{text}: F64 immediates store only the high 32 bits; use raw hex bits')
        return bits >> 32
    return bits


class ExplicitView(pydecode.View):
    def __init__(self, md, env, widths, lexemes):
        super().__init__(md, env, widths)
        self.lexemes = lexemes

    def value(self, a, item):
        if a.name in self.lexemes:
            return self.lexemes[a.name], False
        return super().value(a, item)

    def target(self, a, off):
        if a.name in self.lexemes:
            return self.lexemes[a.name]
        return super().target(a, off)


@cache
def mnemonic_index(arch):
    out = {}
    for k in ir.load(arch).classes:
        out.setdefault(k.mnemonic.split('.')[0].upper(), []).append(k)
    return out


def assemble(arch, text, addr=0, schedule=None, symbols=None):
    """Internal result: bytes, selected class, environment, widths."""
    md = ir.load(arch)
    source = text.strip().removesuffix(';').strip()
    if addr < 0 or addr % 16:
        raise ValueError('instruction address must be a nonnegative multiple of 16')
    if match := re.search(r'\s*@sched\(([^()]*)\)\s*$', source):
        schedule = (schedule or {}) | parse_schedule(match[1])
        source = source[:match.start()].strip()
    head = re.match(r'(?:@!?\w+\s+)?([\w]+)', source)
    if not head:
        raise ValueError('expected a SASS instruction')
    choices = mnemonic_index(arch).get(head[1].upper(), ())
    if not choices:
        raise ValueError(f'unknown opcode {head[1]!r} for {arch}')
    successes, errors = [], []
    for k in choices:
        try:
            templates = patterns(arch, k.name)
        except ValueError as exc:
            errors.append(f'{k.name}: {exc}')
            continue
        for pattern, atoms, flags, desc in templates:
            match = pattern.fullmatch(source)
            if match is None:
                continue
            try:
                values, lexemes = {}, {}
                for i, atom in enumerate(atoms):
                    token = match.groupdict().get(f'v{i}')
                    if token is None:
                        continue
                    lexemes[atom.name] = token
                    etype = next((t for n, t, _, _ in k.aliases if n == atom.name), atom.type)
                    if etype in md.enums:
                        value = next(v for n, v in md.enums[etype].items() if n.upper() == token.upper())
                    elif atom.type in pydecode.PREFIX:
                        continue
                    elif atom.type == 'BITSET':
                        indices = [int(n) for n in token.strip('{} ').split(',') if n.strip()]
                        width = int(atom.args.split('/')[0])
                        if len(set(indices)) != len(indices) or any(n >= width for n in indices):
                            raise ValueError(f'{atom.name}: expected distinct bit indices below {width}')
                        value = sum(1 << n for n in indices)
                    elif (m := pydecode.IMM.fullmatch(atom.type)) and m['float']:
                        value = token  # convert after modifiers have selected convertFloatType
                    else:
                        if atom.type == 'RSImm' and re.fullmatch(r'[A-Za-z_.$][\w.$]*', token):
                            if token not in (symbols or {}):
                                raise ValueError(f'undefined symbol {token}')
                            value = symbols[token]
                        else:
                            value = encoding.literal(token)
                        if atom.type == 'RSImm':
                            if value % 16:
                                raise ValueError(f'{atom.name}: branch target must be aligned to 16 bytes')
                            value -= addr + 16
                    values[atom.name] = value
                for group, key in flags.items():
                    values[key] = int(bool(match.groupdict().get(group)))
                for a in k.format:
                    if a.type == 'EXP_DESC':
                        values[a.name] = desc
                values.update(encoding.schedule_values(md, k, schedule or {}))
                env = encoding.defaults(md, k) | values
                for f, rule in k.rules:
                    if rule[0] == 'op' and rule[3] and isinstance(values.get(rule[1]), str):
                        kind = pydecode.float_kind(md, env, rule[3])
                        values[rule[1]] = float_bits(values[rule[1]], kind)
                for a in atoms:
                    if isinstance(values.get(a.name), str):
                        m = pydecode.IMM.fullmatch(a.type)
                        if m and m['float']:
                            values[a.name] = float_bits(values[a.name], m['float'])
                data, env, widths = encoding.encode_class(md, k, values)
                env['__addr'] = addr
                view = ExplicitView(md, env, widths, lexemes)
                guard, items = pydecode.render_items(k, view)
                rendered = guard + items[0] + (' ' + ', '.join(t for t in items[1:] if t) if any(items[1:]) else '')
                if normalize(rendered) != normalize(source):
                    raise ValueError('operand punctuation or modifier placement does not match FORMAT')
                successes.append((data, k, env, widths))
            except (ValueError, pydecode.NoMatch) as exc:
                errors.append(f'{k.name}: {exc}')
    if not successes:
        detail = '; '.join(dict.fromkeys(errors)) if errors else 'operand syntax does not match any form'
        raise ValueError(f'{source}: {detail}')
    words = {s[0] for s in successes}
    if len(words) > 1:
        # Syntax can have synonymous encodings. Only accept them if the canonical decoder
        # agrees on all visible operand text; scheduling requests have already been checked.
        try:
            texts = {normalize(pydecode.decode(arch, w, addr).text) for w in words}
        except pydecode.NoMatch:
            texts = set()
        if len(texts) != 1:
            raise ValueError('ambiguous instruction: ' + ', '.join(s[1].name for s in successes))
    return min(successes, key=lambda s: (s[1].alternate, s[1].order, int.from_bytes(s[0], 'little')))


def parse_schedule(text):
    out = {}
    if not text.strip():
        return out
    for part in text.split(','):
        if not re.fullmatch(r'\s*\w+\s*=\s*[\w+-]+\s*', part):
            raise ValueError(f'invalid scheduling assignment {part!r}')
        name, value = map(str.strip, part.split('='))
        if name in out:
            raise ValueError(f'duplicate scheduling operand {name}')
        out[name] = value
    return out
