from sass.semantics.bitvec import const, extract, ite, load, sext, trunc, zext
from sass.semantics.family import effect, family, outputs, unmodelled
from sass.semantics.families import memory


@family("LDGSTS", modifier_roles={"loc": "hint", "cop": "hint", "sp2": "hint",
                                 "sem": "effect", "sco": "effect", "private": "effect"})
def ldgsts(i):
    width, _ = memory.size(i)
    if i.select_fork("sem") == "MMIO": unmodelled("async MMIO environment")
    i.select_fork("loc")
    fill = str(i.select_fork("fc"))
    if fill not in ("ZFILL", "nofillctrl"): unmodelled(f"async fill {fill}")
    source = memory.address(i)
    address = i.value("Rb", modifiers=False) + sext(i.imm("Rb_offset"), 32)
    if i.has_operand("Rb_URc"): address = address + trunc(i.value("Rb_URc", modifiers=False), 32)
    enable = i.pred("Pnz")
    value = ite(enable, load("ram", source, width), const(0, width))
    low = extract(value, 63, 0) if width > 64 else zext(value, 64)
    high = extract(value, 127, 64) if width == 128 else const(0, 64)
    return outputs(Pnz=i.pred("Pnz", modifiers=False), effects=[memory.ordering(i), *memory.descriptor(i),
                            effect("sass_async_issue", zext(address, 64), low, high, const(width // 8, 32),
                                   const(1, 1))])


@family("LDGDEPBAR")
def commit(i):
    return outputs(effects=[effect("sass_async_commit")])


@family("DEPBAR")
def wait(i):
    board, count = const(7, 32), const(0, 32)
    if i.has_operand("le"):
        if i.select_fork("le") != "LE": unmodelled("dependency comparison")
        board = i.encoding("sbidx")
        count = i.value("cnt|URb", modifiers=False)
    return outputs(effects=[effect("sass_async_wait", board, count, i.encoding("scoreboard_list"))])
