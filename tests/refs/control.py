"""Single-instruction control flow: relative displacements start after the 16-byte instruction."""


def counter(pc, target=None, relative=False, taken=True):
    if target is None or not taken: return (pc + 16) & ((1 << 64) - 1)
    return (target + (pc + 16 if relative else 0)) & ((1 << 64) - 1)


def divergence_branch(cond, mask, waiting, pp=True):
    """Whether BRA.DIV/.CONV with lane mask `mask` is taken (SM89 GPU, research/bra_div). A lane is
    diverged when the mask names it and it is alive without executing the branch (`waiting`, e.g. at a
    convergence barrier); exited lanes never are. DIV is taken with a diverged lane, CONV without one,
    neither when Pp is false."""
    diverged = any(mask >> lane & waiting >> lane & 1 for lane in range(32))
    return bool(pp) and diverged == (cond == "DIV")
