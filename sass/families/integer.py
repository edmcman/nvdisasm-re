"""Integer families."""
from sass.sem import *
from sass.sem_family import family, outputs, unmodelled


@family('IADD3', 'UIADD3')
def iadd3(i):
    """Rd = Ra + Rb + Rc, with carries at 2^32 (Pu) and 2^33 (Pv); .X adds carry-ins Pp and Pq.
    Plain IADD3 can negate a source, IADD3.X invert one (the md forbids modifying both Ra and Rb)."""
    x = i.has_operand('X')
    modified = i.operand_inverted if x else i.operand_negated

    def term(names):
        w = zext(i.value(names, modifiers=False), 64)
        if not modified(names): return w
        return w ^ 0xffffffff if x else (w ^ 0xffffffff) + 1   # -0 still carries 2^32

    total = term('Ra') + term('Rb|Sb') + term('Rc|Sc')
    if x: total = total + zext(i.pred('Pp'), 64) + zext(i.pred('Pq'), 64)
    return outputs(Rd=trunc(total, 32), Pu=total >= 1 << 32, Pv=total >= 1 << 33,
                   order=('Pv', 'Pu'))  # aliased Pu/Pv: Pu wins (SM89)


@family('BMSK')
def bmsk(i):
    """A mask of `width` ones starting at bit `pos`; .W wraps both to 5 bits, .C clamps the width."""
    pos, width = i.value('Ra'), i.value('Rb|Sb')
    match i.sel('cw'):
        case 'W': pos, width = pos & 31, width & 31
        case 'C': width = umin(width, 32)
        case _: unmodelled('cw selector')
    ones = const(0xffffffff, 32)
    return outputs(Rd=0 if pos >= 32 else (ones << pos) & ~(ones << (pos + width)))
