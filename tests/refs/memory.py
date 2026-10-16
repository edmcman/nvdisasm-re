"""Generic addressing, from SM89 GPU observation (docs/hardware-facts.md): the shared and local
windows are [base, base + size) with base SR_*WINHI:SR_*WINLO and size SR_*WINSZ; a generic
address inside one is that space's byte at its distance from the base, any other is global."""


def generic(address, shared, local):
    """(space, address) a generic byte address names; shared and local are (base, size) windows."""
    for space, (base, size) in (("shared", shared), ("localmem", local)):
        if base <= address < base + size: return space, address - base
    return "ram", address


def generic_kernel(a, b, c):
    """sem_generic: little-endian bytes of a with byte 1 replaced by b's low byte (ST, ST.U8), read
    back as the word w (LD) and the sign-extended high half h (LD.S16); w ^ h + 3 * w, any space."""
    cell = bytearray(a.to_bytes(4, "little")); cell[1] = b & 0xff
    w = int.from_bytes(cell, "little"); h = int.from_bytes(cell[2:], "little", signed=True) & 0xffffffff
    return ((w ^ h) + 3 * w) & 0xffffffff


def descriptor_neutral(descriptor):
    """Whether an access through 64-bit L2 policy descriptor `descriptor` behaves exactly as without
    one (SM89 GPU: every descriptor with bit 59 clear did). Bit 59 marks the range form, which traps
    for some values; which is unknown, so those are not judged (None)."""
    return None if descriptor >> 59 & 1 else True


def desc_kernel(a, b, c):
    """sem_desc: every access as without its descriptor; w = back = a ^ b, (w ^ c) + 3 * back."""
    v = a ^ b
    return ((v ^ c) + 3 * v) & 0xffffffff


def desc_param_kernel(a, b, c):
    """sem_desc_param, whose descriptor never changes a value: 3a + 1."""
    return (3 * a + 1) & 0xffffffff


def pnz_kernel(a, b, c):
    """sem_pnz with LD's Pnz from c's low bit: a for odd c, else 0."""
    return zero_filled(a, c & 1)


def zero_filled(value, pnz):
    """A load with predicate input Pnz (after its `!`): the value when Pnz is true, else 0 (SM89 GPU:
    nothing is read, so an unmapped address doesn't trap)."""
    return value if pnz else 0
