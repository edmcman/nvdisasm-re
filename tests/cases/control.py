"""Control flow, special registers, warp and synchronization context, opaque fallbacks and
whole-kernel execution limits."""

from tests.harness.manual import retained


@retained("Hardware special-register anchors, opaque guards, runtime synchronization events and kernel termination errors exercise contracts beyond generated value/PC profiles.")
def emulator_cases(c):
    # Physical zero-SR pair: seeded destinations prove overwrite; generated SR profiles use synthetic state.
    c.add("cs2r_", dict(Rd=8, SRa="SRZ", sz="64"), dict(R8=1, R9=2), dict(R8=0, R9=0))
    c.add('s2ur_', dict(URd=8, SRa='SR_CTAID.X'), {'SR_CTAID.X':77}, dict(UR8=77, __native=True),
          hardware={'adapter':'ctaid'})
    c.add("r2ur__noOR", dict(URd=8, Ra=2), dict(R2=0x89abcdef), dict(UR8=0x89abcdef, __native=True))
    c.add("r2ur__noOR", dict(URd=8, Ra=2, Pu=0), dict(R2=1), dict(__opaque="sass_opaque_R2UR"))
    # The physical branch adapter currently supports this exact displacement.
    c.add("bra_", dict(sImm=32), {}, dict(__counter=0x1030))
    c.add("bra_", dict(sImm=32, Pp=0), dict(P0=0), dict(__counter=0x1010))
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


def branch(a, b, c):
    """sem_branch: a, then b & 7 rounds of r = 3r + c."""
    for _ in range(b & 7): a = (3 * a + c) & 0xffffffff
    return a


def gpu_fixtures():
    from tests.harness import scalar
    return [scalar.launch("branch", branch)]
