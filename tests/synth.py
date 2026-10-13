"""Synthetic words per md class: constraints satisfied, enum/table fields valid, the rest random."""
import random
from sass import ir, pydecode
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

def legal_word(arch, k, rng, seed, tries=64):
    """A word of k that satisfies the md CONDITIONS: the usual draw, else redraws from the class's
    own generator (so other classes keep their words); the usual draw if none is legal."""
    first = w = word_for(arch, k, rng)
    redraw = random.Random(f"{seed}:{k.name}")
    for _ in range(tries):
        try: pydecode.decode(arch.name, w); return w
        except pydecode.NoMatch: w = word_for(arch, k, redraw)
    return first

def synthetic(archname, per_class=64, seed=0, classes=None, legal=False):
    arch, rng = ir.load(archname), random.Random(seed)
    draw = (lambda k: legal_word(arch, k, rng, seed)) if legal else (lambda k: word_for(arch, k, rng))
    return {draw(k): f"synth:{k.name}" for k in arch.classes
            if classes is None or k.name in classes for _ in range(per_class)}
