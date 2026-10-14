"""Control flow, special registers, warp and synchronization context, opaque fallbacks and
whole-kernel execution limits."""


def emulator_cases(c):
    c.add("s2r_", dict(Rd=8, SRa=0), dict(SR_LANEID=123), dict(R8=123))
    c.add("cs2r_", dict(Rd=8, SRa=0, sz="64"), dict(SR_LANEID=123, SR_CLOCK=4), dict(R8=123, R9=4))
    c.add("cs2r_", dict(Rd=8, SRa="SRZ", sz="64"), dict(R8=1, R9=2), dict(R8=0, R9=0))
    c.add("s2ur_", dict(URd=8, SRa="SR_CTAID.X"), {"SR_CTAID.X": 77}, dict(UR8=77, __native=True))
    c.add("r2ur__noOR", dict(URd=8, Ra=2), dict(R2=0x89abcdef), dict(UR8=0x89abcdef, __native=True))
    c.add("r2ur__noOR", dict(URd=8, Ra=2, Pu=0), dict(R2=1), dict(__opaque="sass_opaque_R2UR"))
    c.add("bra_", dict(sImm=32), {}, dict(__counter=0x1030))
    c.add("bra_", dict(sImm=32, Pp=0), dict(P0=0), dict(__counter=0x1010))
    c.add("call_abs__RIR", dict(Sa=0x4560, depth="NOINC"), {}, dict(__counter=0x4560))
    c.add("ret__ABS", dict(Ra=2, Ra_offset=0, depth="NODEC"), dict(R2=0x4560, R3=0), dict(__counter=0x4560))
    # RET.REL from 0x1000: back to the function start (0x1000 - 0x480) plus the stored offset 0x170.
    c.add("ret__REL", dict(Ra=4, Ra_offset=-0x490, depth="NODEC"), dict(R4=0x170, R5=0), dict(__counter=0xcf0))
    c.add("exit_", {}, {}, dict(__counter=0))
    warp = dict(values=list(range(32)), predicates=[i % 2 for i in range(32)], lane=3, active=0xffffffff)
    c.add("shfl__RRR", dict(Rd=8, Pu=0, Ra=2, Rb=4, Rc=6, shflmd="IDX"), dict(R2=3, R4=5, R6=31), dict(R8=5, P0=1), context={"warp": warp})
    c.add("shfl__RRR", dict(Rd=8, Pu=0, Ra=2, Rb=4, Rc=6, shflmd="IDX"), dict(R2=3, R4=5, R6=31), dict(error="inactive shuffle source"),
          context={"warp": dict(warp, active=0xffffffdf)})
    c.add("vote_", dict(Rd=8, Pu=0, Pp=1, voteop="ANY"), dict(P1=1), dict(R8=0xaaaaaaaa, P0=1), context={"warp": warp})
    c.add("bssy_", dict(Pp=0), dict(P0=0), dict(__events=0))
    c.add("bssy_", {}, {}, dict(error="synchronization context required"))
    c.add("bsync_", {}, {}, dict(__events=1), context={"synchronization": {"bsync": True}})
    c.add("bar__SYNCALL_noSrc_II", {}, {}, dict(__events=1), context={"synchronization": {"bar": True}})
    # Convergence-barrier state and warp synchronization come from the environment, like BSSY/BSYNC.
    c.add("bmov_clear__Rd", dict(Rd=8, cbu_state="B0"), {}, dict(R8=0x55, __events=1), context={"synchronization": {"bmov": {"B0": 0x55}}})
    c.add("bmov_clear__Rd", dict(Rd=8, cbu_state="B0"), {}, dict(error="synchronization context required"))
    c.add("break_inst_", dict(barReg="B1"), {}, dict(__events=1), context={"synchronization": {"break": True}})
    c.add("warpsync__RIR", dict(Sb=0xffffffff), {}, dict(__events=1), context={"synchronization": {"warpsync": True}})
    c.add("mufu__RRR_RR", dict(Rd=8, Rb=2), dict(R2=0x3f800000), dict(error="MUFU approximation"))
    for name, cls, fields, message in [("loop_limit", "bra_", dict(sImm=-16), "kernel instruction limit"),
                                       ("zero_target", "bra_", dict(sImm=-0x100010), "terminated without EXIT"),
                                       ("fallthrough", "nop_", {}, "PC outside kernel")]:
        c.kernel(name, cls, fields, message)


def gpu_fixtures():
    from tests.harness import scalar
    return scalar.generic("branch")
