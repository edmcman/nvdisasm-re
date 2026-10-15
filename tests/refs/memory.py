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
