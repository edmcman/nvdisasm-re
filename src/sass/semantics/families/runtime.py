"""Operations executed by runtime primitives (src/sass/semantics/runtime.py): warp collectives (SHFL,
VOTE/VOTEU, MATCH, REDUX, ELECT), synchronization
and convergence barriers, hardware approximations, the QSPC address-space query; and
UP2UR, which packs uniform predicates."""
from sass.semantics.bitvec import concat, const, extract, ite, reg, zext
from sass.semantics.family import family, outputs, primitive, unmodelled


@family("MUFU", "SHFL", "VOTE", "VOTEU", "MATCH", "REDUX", "BAR", "BSSY", "BSYNC", "BMOV", "BREAK", "WARPSYNC", fallback="prim")
def runtime(i):
    """Multi-thread, synchronization or approximation semantics: every encoding goes to the
    runtime primitive, which validates its selectors (unsupported combinations raise there)."""
    primitive()


@family("QSPC")
def qspc(i):
    """Pu = whether Ra + offset is in the queried space, Rd its space's one-hot tag (G=1, S=2, L=4,
    SM89 GPU), from the emulator's address-space map. .D is modelled only with Rd = RZ (its
    nonzero-Rd tag is not hardware-verified)."""
    for n in ("e", "input_reg_sz_32_dist"):   # spans the runtime reads from the assembled inputs
        if i.has_operand(n): i.select_fork(n)
    space = i.select_fork("space")
    if space in ("G", "L", "S") or space == "D" and i.is_zero("Rd"): primitive("address-space query")
    unmodelled(f"space {space}")


@family("ELECT")
def elect(i):
    """Pu = this lane leads: the lowest active lane in URa's mask (~URa with its invert bit), URd =
    the leader's lane id (PTX elect.sync; no hardware here). The Pp form and .IGNOREKILL are unmodelled."""
    if i.has_operand("Pp"): unmodelled("Pp form")
    if i.select_fork("ignoreKill") != "noignore_kill": unmodelled("IGNOREKILL")
    primitive("warp election")


@family("UP2UR")
def up2ur(i):
    """URd = URa | UPk << k for each bit k of the immediate mask (UP0-UP6; higher bits ignored)."""
    if i.cls != "up2ur__Imm": unmodelled("register mask")
    if i.select_fork("insert") != "B0": unmodelled("insert byte")
    i.select_fork("UPR")   # the one-label source operand naming the uniform predicate file
    mask = i.static_low("Sb", 7)
    v = i.value("URa", modifiers=False)
    for k in range(7):
        if mask >> k & 1: v = v | (zext(reg("UP", k, 1, element=1), 32) << k)
    return outputs(URd=v)


@family("HFMA2.MMA")
def hfma2_mma(i):
    """The constant move HFMA2.MMA Rd, -RZ, RZ, imm: each immediate half, a NaN as 0x7fff (SM89
    GPU: zeros, infinities and denormals kept). Computed while decoding, so one COPY. Other MMA
    forms need their own hardware evidence."""
    if not all(i.has_operand(n) for n in ("Ra", "Rb", "Sc", "Sc1")): unmodelled("operand form")
    if not (i.is_zero("Ra") and i.operand_negated("Ra") and i.is_zero("Rb") and not i.operand_negated("Rb")):
        unmodelled("not the -RZ, RZ constant move")
    canonical = lambda h: ite((extract(h, 14, 10) == 0x1f) & (extract(h, 9, 0) != 0), const(0x7fff, 16), h)
    return outputs(Rd=concat(canonical(i.imm("Sc", 16)), canonical(i.imm("Sc1", 16))))
