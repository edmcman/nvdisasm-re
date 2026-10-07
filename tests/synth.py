"""Synthetic words per md class: constraints satisfied, enum/table fields valid, the rest random."""
import random
from sass import ir
from sass.pydecode import resolve

def field_value(arch, k, f, rule, rng):
    match rule:
        case ("op", name, _, _) if (a := k.operand_types.get(name)) and a.type in arch.enums:
            vals = [v for v in set(arch.enums[a.type].values()) if 0 <= v < 1 << f.width]
            return rng.choice(vals) if vals else None
        case ("table", tname, _) if tname in arch.tables:
            outs = [o for o in (resolve(arch, r) for _, r in arch.tables[tname]) if isinstance(o, int) and 0 <= o < 1 << f.width]
            return rng.choice(outs) if outs else None
    return None

def word_for(arch, k, rng):
    w = rng.getrandbits(128) & ~k.constraint_mask | k.constraint_bits
    for f, rule in k.rules:
        if (v := field_value(arch, k, f, rule, rng)) is not None:
            w = w & ~f.mask | f.put(v)
    return w.to_bytes(16, "little")

def synthetic(archname, per_class=64, seed=0, classes=None):
    arch, rng = ir.load(archname), random.Random(seed)
    return {word_for(arch, k, rng): f"synth:{k.name}" for k in arch.classes
            if classes is None or k.name in classes for _ in range(per_class)}
