"""Synthetic words per md class: constraints satisfied, enum/table fields valid, the rest random."""
import random
from sass.decode import ir, pydecode
from sass.decode.pydecode import resolve

REGISTERS = ("Register", "NonZeroRegister", "UniformRegister", "NonZeroUniformRegister")


def field_value(arch, k, f, rule, rng, aligned=False):
    match rule:
        case ("op", name, _, _) if (a := k.operand_types.get(name)) and a.type in arch.enums:
            vals = [v for v in set(arch.enums[a.type].values()) if 0 <= v < 1 << f.width]
            if aligned and a.type in REGISTERS:
                # Registers that satisfy any md range and alignment condition (wide spans, UR0-UR63),
                # and the zero register (the highest value), which some forms require (HADD2.F32 -RZ).
                vals = sorted(v for v in vals if v % 8 == 0 and v < 64 or v == max(vals)) or vals
            return rng.choice(vals) if vals else None
        case ("table", tname, _) if tname in arch.tables:
            outs = [o for o in (resolve(arch, r) for _, r in arch.tables[tname]) if isinstance(o, int) and 0 <= o < 1 << f.width]
            return rng.choice(outs) if outs else None
    return None

def word_for(arch, k, rng, aligned=False):
    w = rng.getrandbits(128) & ~k.constraint_mask | k.constraint_bits
    for f, rule in k.rules:
        if (v := field_value(arch, k, f, rule, rng, aligned)) is not None:
            w = w & ~f.mask | f.put(v)
    return w.to_bytes(16, "little")

def legal_word(arch, k, rng, seed, tries=256):
    """A word of k that satisfies the md CONDITIONS: the usual draw, else redraws from the class's
    own generator with aligned low registers (so other classes keep their words); the usual draw if
    none is legal."""
    first = w = word_for(arch, k, rng)
    redraw = random.Random(f"{seed}:{k.name}")
    for _ in range(tries):
        try: pydecode.decode(arch.name, w); return w
        except pydecode.NoMatch: w = word_for(arch, k, redraw, aligned=True)
    return first

def synthetic(archname, per_class=64, seed=0, classes=None, legal=False):
    arch, rng = ir.load(archname), random.Random(seed)
    draw = (lambda k: legal_word(arch, k, rng, seed)) if legal else (lambda k: word_for(arch, k, rng))
    return {draw(k): f"synth:{k.name}" for k in arch.classes
            if classes is None or k.name in classes for _ in range(per_class)}
