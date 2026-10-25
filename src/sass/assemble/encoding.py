"""Class-local encoding, including alternate syntax and scheduling operands.

Assembly is strict: it never truncates, chooses a nearby table row or writes an illegal word.
`encode`/`word` are the harness-facing API over the same encoder; `permissive=True` accepts
synthetic edge cases (out-of-range or misaligned values, illegal combinations) by truncating
and picking the nearest table row, and still writes the md's starred literals.
"""
import itertools
import re
from functools import cache

from sass.decode import ir, pydecode
from sass.md import legal, parse
from sass.sleigh.gen import default_value
from sass.sleigh.registers import ENUM_FILE, REG_FILES


@cache
def klass(arch, name):
    return next(c for c in ir.load(arch).classes if c.name == name)


@cache
def scheduling(arch, name):
    text = parse.arch_classes(arch)[name]['sections']['FORMAT']
    atoms = []
    for group in re.findall(r'\$\((.*?)\)\$', text, re.S):
        for a in ir.parse_format(group):
            if a.kind not in ('operand', 'mod'):
                continue
            if a.default is None and (m := re.match(r'"([^"]+)"/', a.args)):
                from dataclasses import replace
                a = replace(a, default=m[1])
            atoms.append(a)
    return tuple(atoms)


@cache
def ignored_bits(arch, name):
    text = parse.arch_classes(arch)[name]['sections']['ENCODING']
    return tuple((ir.parse_field(lhs), int(value, 0)) for lhs, value in re.findall(
        r'^(BITS_\w+)\s*=\s*\*\s*(0[xX][\da-fA-F]+|0[bB][01]+|\d+)\s*;', text, re.M))


def defaults(md, k):
    env = {}
    for a in [*k.operand_types.values(), *scheduling(md.name, k.name)]:
        v = default_value(md, a)
        if v is None and a.type in md.enums:
            v = min(md.enums[a.type].values())
        if v is not None:
            env[a.name] = v
    for f, r in k.rules:
        if r[0] == 'attr':
            env.setdefault((r[1], r[2]), 0)
        if r[0] == 'table':
            for a in r[2]:
                if a[0] == 'attr':
                    env.setdefault(pydecode.key(a), 0)
    return env


def literal(value):
    return int(value, 0) if not re.fullmatch(r'[+-]?\d+', value) else int(value, 10)


def schedule_values(md, k, values):
    atoms = {a.name: a for a in scheduling(md.name, k.name)}
    out = {}
    for name, text in values.items():
        if name not in atoms:
            raise ValueError(f'unknown scheduling operand {name!r} for {k.name}')
        atom = atoms[name]
        if atom.type in md.enums:
            if text not in md.enums[atom.type]:
                raise ValueError(f'{name}: expected a {atom.type} label, got {text!r}')
            out[name] = md.enums[atom.type][text]
        else:
            try:
                out[name] = literal(text)
            except ValueError:
                raise ValueError(f'{name}: expected an integer, got {text!r}') from None
    return out


def alias_environments(md, k, env):
    """Invert pseudo-op pattern tables; wildcards leave the operand unconstrained."""
    options = [env]
    for name, etype, table, args in k.aliases:
        wanted = env.get(name, md.enums[etype].get(k.operand_types[name].default, 0))
        expanded = []
        for ins, out in md.tables[table]:
            if pydecode.resolve(md, out) != wanted:
                continue
            for current in options:
                candidate = current.copy()
                for arg, tok in zip(args, ins):
                    if tok == '-':
                        continue
                    value = pydecode.resolve(md, tok)
                    if arg in candidate and candidate[arg] != value:
                        break
                    candidate[arg] = value
                else:
                    expanded.append(candidate)
        options = expanded
    return options


def encode_class(md, k, requested, base=None):
    """Return one deterministic exact encoding and its class-local decoded environment."""
    errors = []
    for supplied in alias_environments(md, k, requested):
        try:
            return encode_environment(md, k, supplied, base=base)
        except (ValueError, pydecode.NoMatch) as exc:
            errors.append(str(exc))
    raise ValueError('; '.join(dict.fromkeys(errors)) or 'pseudo-op constraints do not match')


def encode_environment(md, k, requested, permissive=False, base=None):
    desired = (defaults(md, k) if base is None else base) | requested
    # Alias operands describe syntax, not fields. Their actual values are checked below.
    choices = []
    for f, r in k.rules:
        match r:
            case ('op', name, scale, conv):
                if name not in desired and not permissive:
                    raise ValueError(f'{name}: missing required operand')
                value = desired.get(name, 0)
                atom = k.operand_types.get(name)
                signed = atom is not None and atom.type in ('SImm', 'RSImm')
                limit = (1 << f.width) * scale
                lo, hi = (-limit // 2, limit // 2) if signed else (0, limit)
                if not permissive and not lo <= value < hi:
                    raise ValueError(f'{name}: {value} outside [{lo}, {hi - 1}]')
                if not permissive and value % scale:
                    raise ValueError(f'{name}: {value} must be aligned to {scale} bytes')
                choices.append((f, [f.put((value // scale) % (1 << f.width))]))
            case ('attr', name, attr):
                value = desired.get((name, attr), 0)
                if not permissive and not 0 <= value < 1 << f.width:
                    raise ValueError(f'{name}@{attr}: out of range')
                choices.append((f, [f.put(value % (1 << f.width))]))
            case ('table', 'IDENTICAL', args) if permissive:
                choices.append((f, [f.put(desired.get(pydecode.key(args[0]), 0) % (1 << f.width))]))
            case ('table', table, args) if permissive:
                wanted = tuple(desired.get(pydecode.key(a), 0) for a in args if a[0] != 'const')
                choices.append((f, [f.put(table_raw(md.name, table, args, wanted) % (1 << f.width))]))
            case ('table', 'IDENTICAL', args):
                vals = [a[1] if a[0] == 'const' else desired.get(pydecode.key(a)) for a in args]
                present = [v for v in vals if v is not None]
                if not present or len(set(present)) != 1 or not 0 <= present[0] < 1 << f.width:
                    raise ValueError(f'IDENTICAL: conflicting operands {vals}')
                choices.append((f, [f.put(present[0])]))
            case ('table', table, args):
                raws = []
                for raw, rows in pydecode.table_inverse(md, table).items():
                    if not isinstance(raw, int) or not 0 <= raw < 1 << f.width:
                        continue
                    if any(all(a[1] == v if a[0] == 'const' else
                               desired.get(pydecode.key(a), v) == v for a, v in zip(args, row))
                           for row in rows):
                        raws.append(f.put(raw))
                if not raws:
                    raise ValueError(f'{table}: no exact row for requested operands')
                choices.append((f, sorted(set(raws))))
            case _:
                pass
    # A field can overlap another field or fixed bits: every writer must agree.
    last_error = 'conflicting encoding fields or fixed bits'
    for combination in itertools.product(*(vs for _, vs in choices)):
        word, used = k.constraint_bits, k.constraint_mask
        for f, value in ignored_bits(md.name, k.name):
            word |= f.put(value) & ~used
        for (f, _), bits in zip(choices, combination):
            if not permissive and (word ^ bits) & used & f.mask:
                break
            word = (word & ~f.mask) | bits
            used |= f.mask
        else:
            try:
                env, widths = pydecode.decode_env(md, k, word)
                if permissive:
                    got = pydecode.decode(md.name, word.to_bytes(16, 'little'), conditions=False).klass
                    # A pseudo alias (IMAD.MOV for IMAD with a zero source) is the same instruction printed differently.
                    if got.name != k.name and not (got.alternate and (got.constraint_mask, got.constraint_bits) == (k.constraint_mask, k.constraint_bits)):
                        raise ValueError(f'{k.name} encodes as {got.name}')
                    return word.to_bytes(16, 'little'), env, widths
                if bad := legal.violation(md, k, env):
                    raise ValueError(f'{bad[0]}: {bad[1]}')
                env['__strict'] = k.strict
                # Rendering checks enum membership as well as types selected by aliases.
                pydecode.apply_aliases(md, k, env, widths)
                pydecode.render(md, k, env | {'__addr': 0}, widths)
                for name, value in requested.items():
                    actual = env.get(name)
                    if actual is None:
                        # Prefix tokens and zero-width syntax-only modifiers have no encoding.
                        if name in k.operand_types and k.operand_types[name].type in pydecode.PREFIX:
                            continue
                        if name in k.operand_types and k.operand_types[name].type in md.enums and len(set(md.enums[k.operand_types[name].type].values())) == 1:
                            continue
                        if name in k.operand_types and default_value(md, k.operand_types[name]) == value:
                            continue
                        # Some FORMAT operands have no field (e.g. TXQ tid8). The decoder
                        # renders an absent integer as zero; accept only that actual value.
                        if name in k.operand_types and pydecode.IMM.fullmatch(k.operand_types[name].type) and value == 0:
                            continue
                        raise ValueError(f'{name}: operand is not encoded')
                    if value < 0 and isinstance(name, str) and name in widths:
                        actual = pydecode.sext(actual, widths[name])
                    if actual != value:
                        raise ValueError(f'{name}: encoding changes {value} to {actual}')
                return word.to_bytes(16, 'little'), env, widths
            except (ValueError, pydecode.NoMatch) as exc:
                last_error = str(exc)
    raise ValueError(last_error)


@cache
def table_raw(arch, table, args, wanted):
    """The raw table-field value whose row matches the wanted argument values best (fewest
    mismatches, then the smallest raw value); `wanted` lists the non-constant arguments in order."""
    candidates = []
    for raw, rows in pydecode.table_inverse(ir.load(arch), table).items():
        for row in rows:
            if all(a[0] != "const" or a[1] == v for a, v in zip(args, row)):
                got = [v for a, v in zip(args, row) if a[0] != "const"]
                candidates.append((sum(w != v for w, v in zip(wanted, got)), raw))
    if not candidates: raise ValueError(table)
    return min(candidates)[1]


def harness_defaults(md, k):
    """`defaults`, with guard and predicate operands defaulting to PT."""
    return defaults(md, k) | {
        a.name: REG_FILES[ENUM_FILE[a.type]][3] for a in k.operand_types.values()
        if default_value(md, a) is None and a.type in ENUM_FILE and (a.kind == "guard" or "Predicate" in a.type)}


def encode(arch, cls, *, permissive=False, **values):
    """Hex word for `cls` with operand values (enum labels or numbers); `name@attr` sets modifiers."""
    return encoded(arch, cls, permissive, tuple(sorted(values.items())))


@cache
def encoded(arch, cls, permissive, values):
    """`encode`'s word: sweeps encode the same fields again and again, varying only the registers they seed."""
    values = dict(values)
    md, k = ir.load(arch), klass(arch, cls)
    requested = {tuple(n.split("@")) if "@" in n else n:
                 md.enums[k.operand_types[n].type][v] if isinstance(v, str) else v for n, v in values.items()}
    return encode_environment(md, k, requested, permissive, harness_defaults(md, k))[0].hex()


def word(arch, cls, **values):
    return int.from_bytes(bytes.fromhex(encode(arch, cls, **values)), "little")
