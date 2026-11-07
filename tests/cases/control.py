"""Control flow, special registers, warp and synchronization context, opaque fallbacks and
whole-kernel execution limits."""

import itertools
from tests.harness.manual import retained


@retained("Hardware special-register anchors, opaque guards, missing synchronization-context errors and kernel termination limits exercise contracts beyond generated profiles.")
def emulator_cases(c):
    # Physical zero-SR pair: seeded destinations prove overwrite; generated SR profiles use synthetic state.
    c.add("cs2r_", dict(Rd=8, SRa="SRZ", sz="64"), dict(R8=1, R9=2), dict(R8=0, R9=0))
    c.add('s2ur_', dict(URd=8, SRa='SR_CTAID.X'), {'SR_CTAID.X':77},
          dict(UR8=77, __userops=["sass_ordered_block_barrier_wait_deferred"]),
          hardware={'adapter':'ctaid'})
    c.add("r2ur__noOR", dict(URd=8, Ra=2, Pu=0), dict(R2=1), dict(__opaque="sass_opaque_R2UR"))
    # The physical branch adapter currently supports this exact displacement.
    c.add("bra_", dict(sImm=32), {}, dict(__counter=0x1030))
    c.add("bra_", dict(sImm=32, Pp=0), dict(P0=0), dict(__counter=0x1010))
    c.add("bssy_", {}, {}, dict(error="synchronization context required"))
    # Missing convergence state must fail instead of silently treating BMOV as a no-op.
    c.add("bmov_clear__Rd", dict(Rd=8, cbu_state="B0"), {}, dict(error="synchronization context required"))
    for mode in ("BAR", "WARP", "RESULT"):
        cls = f"b2r__{mode}"
        extra = dict(barname=15) if mode == "BAR" else dict(Pu=0) if mode == "RESULT" else {}
        context = dict(barrier_states=[0] * 15 + [0xa0202001]) if mode == "BAR" else dict(barrier_result=0x800)
        value = {"BAR": 0xa0202001, "WARP": 0, "RESULT": 0x800}[mode]
        message = "sixteen barrier states required" if mode == "BAR" else "barrier reduction result required"
        c.add(cls, dict(Rd=8, **extra), {}, dict(error=message))
        for p0, invert, dest in itertools.product((0, 1), (0, 1), (8, 255)):
            enabled = p0 ^ invert
            expected = {f"R{dest}": (value if enabled else 0x12345678) if dest != 255 else 0,
                        "P0": int(value != 0) if enabled and mode == "RESULT" else p0}
            c.add(cls, dict(Rd=dest, Pg=0, **{"Pg@not": invert}, **extra),
                  dict(R8=0x12345678, P0=p0), expected, context=context,
                  hardware={"skip": "synthetic barrier state; cooperative fixtures establish production"})
    if c.arch != "SM75":
        message = "cache priority address must be 128-byte aligned"
        for predicate, invert in itertools.product((0, 1), (0, 1)):
            enabled = predicate ^ invert
            expected = dict(error=message) if enabled else dict(__events=0)
            c.add("cctl__sImmOffset", dict(cop="DML2", cache="D", e="E", Ra=2, Ra_offset=-1,
                                         Pg=0, **{"Pg@not": invert}),
                  dict(R2=0x100, R3=0x12345678, P0=predicate), expected,
                  hardware={"skip": "misaligned priority request is undefined in PTX"})
    for name, cls, fields, message in [("loop_limit", "bra_", dict(sImm=-16), "kernel instruction limit"),
                                       ("zero_target", "bra_", dict(sImm=-0x100010), "terminated without EXIT"),
                                       ("fallthrough", "nop_", {}, "PC outside kernel")]:
        c.kernel(name, cls, fields, message)

    for index, case in enumerate(c):
        if 'error' in case.expected or '__opaque' in case.expected: continue
        if case.cls == 's2ur_':
            c[index] = case._replace(hardware={'adapter': 'ctaid'})
            continue
        if any(n.startswith('SR_') for n in case.request.get('registers', {})):
            reason = 'synthetic special-register state; hardware registers are read-only'
        elif case.cls == 'bra_':
            c[index] = case._replace(hardware={'adapter': 'branch'})
            continue
        elif '__counter' in case.expected:
            reason = 'isolated emulator PC assertion; hardware control flow is covered by whole-kernel fixtures'
        elif '__events' in case.expected:
            reason = 'emulator runtime-event assertion; synchronization is covered by cooperative fixtures'
        else: continue
        c[index] = case._replace(hardware={'skip': reason})

    c.add('s2r_', dict(Rd=8, SRa='SR_LANEID'), dict(SR_LANEID=3), dict(R8=3),
          hardware={'adapter': 'lane'})
    divergence_branches(c)


def divergence_branches(c):
    """BRA.DIV/.CONV [~]URb against executing lanes, with the others exited or waiting at a
    convergence barrier: equal, nested, disjoint, overlapping, empty and observer-free masks."""
    from tests.refs.control import counter, divergence_branch
    full = 0xffffffff
    scenarios = [(full, full), (full, 0xffff), (0xffff, full), (0xffff, 0xffff), (0xffff, 0xff), (0xffff, 0xff0000),
                 (0xffff, 0xffff00), (0xffff, 0), (0xffff, 0xff00), (1, 1), (1, 3)]
    for (active, mask), wait, cond, invert in itertools.product(scenarios, (0, 1), ("DIV", "CONV"), (0, 1)):
        if active == full and wait: continue
        waiting = ~active & full if wait else 0
        taken = divergence_branch(cond, ~mask & full if invert else mask, waiting)
        c.add("bra_uniform_", {"cond": cond, "URb": 6, "URb@invert": invert, "sImm": 32}, dict(UR6=mask),
              dict(__counter=counter(0x1000, 32, relative=True, taken=taken)),
              context={"warp": dict(lane=0, active=active, waiting=waiting)}, hardware={'adapter': 'warp'})
    for cond, urb, invert, p0, pnot, wait in itertools.product(("DIV", "CONV"), ("URZ", 6), (0, 1), (0, 1), (0, 1), (0, 1)):
        waiting = 0xffff0000 if wait else 0
        mask = 0 if urb == "URZ" else 0xff00   # executing lanes: ~URb, not URb, names waiting ones
        taken = divergence_branch(cond, ~mask & full if invert else mask, waiting, p0 ^ pnot)
        c.add("bra_uniform_", {"cond": cond, "URb": urb, "URb@invert": invert, "Pp": 0, "Pp@not": pnot, "sImm": 32}, dict(UR6=0xff00, P0=p0),
              dict(__counter=counter(0x1000, 32, relative=True, taken=taken)),
              context={"warp": dict(lane=0, active=0xffff, waiting=waiting)}, hardware={'adapter': 'warp'})


def branch(a, b, c):
    """sem_branch: a, then b & 7 rounds of r = 3r + c."""
    for _ in range(b & 7): a = (3 * a + c) & 0xffffffff
    return a


def gpu_fixtures(arch="SM89"):
    from tests.harness import arguments, cache_controls, scalar, synchronization, warp_sync
    return [scalar.launch("branch", branch, arch=arch), arguments.fixture(arch),
            *synchronization.reductions(arch), *synchronization.barrier_states(arch),
            *synchronization.indirect_branches(arch), *cache_controls.fixtures(arch), *warp_sync.fixtures(arch)]
