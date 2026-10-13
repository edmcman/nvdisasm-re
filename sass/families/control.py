"""Control flow: BRA, EXIT, CALL, RET, NOP. Branch-stack depth changes (CALL's default INC, RET's
DEC), divergence-conditional branches (BRA.DIV/.CONV), uniform-register targets and predicates
are unmodelled."""
from sass.sem import const, sext, zext
from sass.sem_family import call, family, goto, outputs, ret, unmodelled


def required(i, name, *labels):
    label = i.select_fork(name)
    if label not in labels: unmodelled(f"{name} {label}")


def condition(i):
    """The branch predicate Pp (with its !), or None when the class has none."""
    return i.pred("Pp") if i.has_operand("Pp") else None


def address(i, names, offset):
    """A 64-bit register address plus its signed offset operand, if any."""
    base = zext(i.value(names, modifiers=False), 64) if i.width(names) < 64 else i.value(names, modifiers=False)
    return base + sext(i.imm(offset), 64) if i.has_operand(offset) else base


@family("BRA")
def bra(i):
    """goto sImm (relative to the next instruction) if Pp; .U is a uniformity hint."""
    if i.has_operand("URb") or i.has_operand("UPq"): unmodelled("uniform target or predicate")
    required(i, "cond", i.default("cond"), "U")
    return outputs(flow=goto(i.branch_target("sImm"), condition(i)))


@family("EXIT")
def exit_(i):
    """The thread ends (return to 0) if Pp; .NO_ATEXIT changes no control flow here."""
    if i.has_operand("Ra|URa"): unmodelled("exit with a register operand")
    if i.has_operand("no_atexit"): required(i, "no_atexit", "nono_atexit", "NO_ATEXIT")
    return outputs(flow=ret(const(0, 64), condition(i)))


@family("CALL")
def call_(i):
    """call Sa (absolute UImm, relative RSImm, or an address loaded from c[][]), or the absolute
    register address Ra + offset, if Pp; only .NOINC (no call-stack push) is modelled."""
    required(i, "depth", "NOINC")
    absolute = i.has_operand("abs")
    required(i, "abs" if absolute else "rel", "ABS" if absolute else "REL")   # one-label selectors
    if i.has_operand("Sa"):
        if i.kind("Sa") == "immediate": return outputs(flow=call(i.branch_target("Sa"), condition(i)))
        return outputs(flow=call(zext(i.value("Sa", modifiers=False), 64), condition(i)))   # c[][]: a loaded address
    if not absolute: unmodelled("relative register target")
    return outputs(flow=call(address(i, "Ra", "Ra_offset|Sa_offset"), condition(i)))


@family("RET")
def ret_(i):
    """return to the absolute register address Ra + offset if Pp; only .NODEC is modelled."""
    required(i, "addr", "ABS"); required(i, "depth", "NODEC")
    return outputs(flow=ret(address(i, "Ra", "Ra_offset|Sa_offset"), condition(i)))


@family("NOP")
def nop(i):
    return outputs()
