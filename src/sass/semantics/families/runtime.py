"""Operations executed by runtime primitives (src/sass/semantics/runtime.py): warp collectives (SHFL,
VOTE/VOTEU, MATCH, REDUX, ELECT), synchronization
and convergence barriers, hardware approximations, the QSPC address-space query."""
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
