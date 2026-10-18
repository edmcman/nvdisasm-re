"""Single-instruction control flow: relative displacements start after the 16-byte instruction."""


def counter(pc, target=None, relative=False, taken=True):
    if target is None or not taken: return (pc + 16) & ((1 << 64) - 1)
    return (target + (pc + 16 if relative else 0)) & ((1 << 64) - 1)
