"""Encode md instruction classes: operand values -> 128-bit words (the inverse of sass.decode.pydecode).

`encode(arch, cls, **values)` takes enum labels or numbers per operand (`name@attr` sets a modifier
bit), fills every unspecified operand with its md default, and checks the round trip through
pydecode: the word must decode as `cls` with the requested values.
"""
from functools import cache
from sass.decode import ir, pydecode
from sass.sleigh.gen import default_value, ENUM_FILE, REG_FILES


@cache
def klass(arch, name):
    return next(c for c in ir.load(arch).classes if c.name == name)


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


def encode(arch, cls, conditions=True, **values):
    """Hex word for `cls` with operand values (enum labels or numbers); `name@attr` sets modifiers.
    conditions=False also encodes what the md's conditions forbid (to test that it is rejected)."""
    md, k = ir.load(arch), klass(arch, cls)
    desired = {}
    for atom in k.operand_types.values():
        value = values.get(atom.name, default_value(md, atom))
        if value is None and atom.type in ENUM_FILE:
            value = REG_FILES[ENUM_FILE[atom.type]][3] if atom.kind == "guard" or "Predicate" in atom.type else 0
        if value is None: value = min(md.enums[atom.type].values()) if atom.type in md.enums else 0
        if isinstance(value, str): value = md.enums[atom.type][value]
        desired[atom.name] = value
    desired.update({tuple(n.split("@")): v for n, v in values.items() if "@" in n})
    word = k.constraint_bits
    for f, r in k.rules:
        match r[0]:
            case "op": raw = desired.get(r[1], 0) // r[2]
            case "attr": raw = desired.get((r[1], r[2]), 0)
            case "table" if r[1] == "IDENTICAL": raw = desired[r[2][0][1]]
            case "table": raw = table_raw(arch, r[1], r[2], tuple(desired.get(pydecode.key(a), 0) for a in r[2] if a[0] != "const"))
            case _: continue
        word = (word & ~f.mask) | f.put(raw & ((1 << f.width) - 1))
    data = ((word & ~k.constraint_mask) | k.constraint_bits).to_bytes(16, "little")
    decoded = pydecode.decode(arch, data, conditions=conditions)
    if decoded.klass.name != cls: raise ValueError((cls, decoded.klass.name))
    for name, value in values.items():
        key = tuple(name.split("@")) if "@" in name else name
        if isinstance(value, str): value = md.enums[k.operand_types[name].type][value]
        if key in decoded.env and decoded.env[key] != value:
            if isinstance(value, int) and value < 0:
                source = next(((f, r[2]) for f, r in k.rules if r[0] == "op" and r[1] == name), None)
                if source and decoded.env[key] == value % ((1 << source[0].width) * source[1]): continue
            raise ValueError((name, value, decoded.env[key]))
    return data.hex()


def word(arch, cls, **values):
    return int.from_bytes(bytes.fromhex(encode(arch, cls, **values)), "little")
