from sass.semantics import ordered
from sass.semantics.bitvec import const, extract, ite, load, sext, trunc, zext
from sass.semantics.family import family, unmodelled
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
    body = i.body()
    i.emit_effects(body, [memory.ordering(i), *memory.descriptor(i)])
    arguments = body.capture_inputs((zext(address, 64), low, high, const(width // 8, 32), const(1, 1)))
    body.special(ordered.ASYNC_ISSUE, *arguments)
    return i.assign_from_inputs(body, Pnz=i.pred("Pnz", modifiers=False))


@family("LDGDEPBAR")
def commit(i):
    body = i.body()
    body.special(ordered.ASYNC_COMMIT, i.control("dst_wr_sb"))
    return body.block()


@family("DEPBAR")
def wait(i):
    body = i.body()
    if i.has_operand("le"):
        if i.select_fork("le") != "LE": unmodelled("dependency comparison")
        arguments = body.capture_inputs((i.encoding("sbidx"), i.value("cnt|URb", modifiers=False)))
        body.special(ordered.ASYNC_WAIT_GROUPS, *arguments)
    body.special(ordered.ASYNC_WAIT_SLOTS, trunc(i.encoding("scoreboard_list"), 6))
    return body.block()
