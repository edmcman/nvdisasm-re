"""Memory: global/shared/local loads and stores, constant loads (LDC/ULDC), shared stride
addressing and QSPC address-space queries."""
from tests.harness.case import cbank


def loads_stores(c):
    for op, space in [("ldg", "ram"), ("lds", "shared"), ("ldl", "localmem")]:
        for sz, data, want in [("S8", "ff", 0xffffffff), ("U16", "3412", 0x1234), ("32", "78563412", 0x12345678),
                               ("64", "0100000002000000", 1), ("128", "01000000020000000300000004000000", 1)]:
            expected = {"R8": want, **({"R9": 2} if sz in ("64", "128") else {}), **(dict(R10=3, R11=4) if sz == "128" else {})}
            c.add(op + "__sImmOffset", dict(Rd=8, Ra=2, Ra_offset=-4, sz=sz), dict(R2=0x104, R3=0), expected,
                  memory=[dict(space=space, address=0x100, hex=data)])
    for op, space in [("stg", "ram"), ("sts", "shared"), ("stl", "localmem")]:
        c.add(op + "__sImmOffset", dict(Ra=2, Ra_offset=0, Rb=4, sz="32"), dict(R2=0x100, R3=0, R4=0x12345678), dict(__memory="78563412"),
              observe_memory=[dict(space=space, address=0x100, size=4)])
    # Shared-memory X4/X8/X16 addressing scales the base register without changing it.
    for stride, factor in [("X4", 4), ("X8", 8), ("X16", 16)]:
        c.add("lds__sImmOffset", dict(Rd=8, Ra=2, Ra_offset=4, stride=stride), dict(R2=0x10), dict(R8=0x12345678),
              memory=[dict(space="shared", address=0x10 * factor + 4, hex="78563412")])
    c.add("ldc__RaNonRZ", dict(Rd=8, Ra=2, Ra_offset=16, Sa_bank=3), dict(R2=0x100), dict(R8=0x12345678), memory=[cbank(3, 0x110, 0x12345678)])
    c.add("mov__RC", dict(Rd=8, Sb_bank=2, Sb_addr=16, PixMaskU04=15), {}, dict(R8=0x12345678), memory=[cbank(2, 16, 0x12345678)])
    for label, data, want in [("U8", "ff", 255), ("S8", "ff", 0xffffffff), ("U16", "ffff", 65535), ("S16", "ffff", 0xffffffff),
                              ("32", "78563412", 0x12345678), ("64", "0100000002000000", 1)]:
        c.add("uldc_const__RCR", dict(URd=8, Sa_bank=2, Sa_addr=16, sz=label), {}, dict(UR8=want, **({"UR9": 2} if label == "64" else {})),
              memory=[dict(space="cbank", address=(2 << 32) | 16, hex=data)])


def qspc(c):
    """QSPC reads generic-address provenance from the emulator's explicit address-space map,
    which uses model ranges rather than process or GPU virtual addresses."""
    context = {"address_spaces": {"global": [[0x100000, 0x100100]], "shared": [[0x200, 0x300]],
                                  "local": [[0x400, 0x500]], "shared_cluster": [[0x600, 0x700]]}}
    if k := c.classes.get("qspc__RaNonRZ"):
        e = k.operand_types["e"]; e = "noe" if "noe" in c.enum(k, "e") else e.default
        fields = dict(Pg="PT", Pu=1, e=e, Ra=2, Ra_offset=0)
        for space, base, offset, expected in (("G", 0x100020, -32, 1), ("S", 0x220, 0, 1), ("L", 0x420, 0, 1), ("G", 0x420, 0, 0)):
            c.add(k.name, dict(fields, Rd="RZ", space=space, Ra_offset=offset), {"R2": base}, {"P1": expected}, context=context)
        c.add(k.name, dict(fields, Rd="RZ", space="G"), {"R2": 0x100000}, dict(error="address_spaces.global context required"))
        for base, expected in ((0x100000, {"P1": 1, "R8": 1}), (0x420, {"P1": 0, "R8": 4}), (0x220, {"P1": 0, "R8": 2})):
            c.add(k.name, dict(fields, Rd=8, space="G"), {"R2": base}, expected, context=context)
        if "D" in c.enum(k, "space"):
            c.add(k.name, dict(fields, Rd="RZ", space="D"), {"R2": 0x620}, {"P1": 1}, context=context)
            c.add(k.name, dict(fields, Rd="RZ", space="D"), {"R2": 0x420}, {"P1": 0}, context=context)
            c.add(k.name, dict(fields, Rd=8, space="D"), {"R2": 0x620}, dict(error="sass_opaque_QSPC"), context=context)
        if k := c.classes.get("qspc_urb__Ra32"):
            e = k.operand_types["e"]; e = "noe" if "noe" in c.enum(k, "e") else e.default
            c.add(k.name, dict(Pg="PT", Pu=1, Rd="RZ", e=e, space="S", Ra=2, input_reg_sz_32_dist="U32", Ra_URb=4, Ra_offset=0),
                  {"R2": 0x100, "UR4": 0x120}, {"P1": 1}, context=context)


def emulator_cases(c):
    loads_stores(c); qspc(c)


def gpu_fixtures():
    from tests.harness import scalar
    return scalar.generic("shared", "local", "ldc")
