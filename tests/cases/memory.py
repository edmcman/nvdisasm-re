"""Memory: global/shared/local loads and stores, generic LD/ST, explicit descriptors, constant loads (LDC/ULDC/LDCU), shared
stride addressing and QSPC address-space queries."""
from tests.harness.manual import retained
from tests.harness.case import cbank
from tests.refs import memory


def loads_stores(c):
    for op, space in [("ldg", "ram"), ("lds", "shared"), ("ldl", "localmem")]:
        for sz, data, want in [("S8", "ff", 0xffffffff), ("U16", "3412", 0x1234), ("32", "78563412", 0x12345678),
                               ("64", "0100000002000000", 1), ("128", "01000000020000000300000004000000", 1)]:
            if op != "ldg" and sz not in ("64","128"): continue  # LDG remains partial; ordinary narrow LDS/LDL accesses are generated.
            expected = {"R8": want, **({"R9": 2} if sz in ("64", "128") else {}), **(dict(R10=3, R11=4) if sz == "128" else {})}
            c.add(op + "__sImmOffset", dict(Rd=8, Ra=2, Ra_offset=-4, sz=sz), dict(R2=0x104, R3=0), expected,
                  memory=[dict(space=space, address=0x100, hex=data)])
    c.add("mov__RC", dict(Rd=8, Sb_bank=2, Sb_addr=16, PixMaskU04=15), {}, dict(R8=0x12345678), memory=[cbank(2, 16, 0x12345678)])
    # Aligned 64-bit constant pairs remain partially native in the manifest and
    # cannot be owned by the planner's ordinary native-form enumeration.
    c.add("uldc_const__RCR", dict(URd=8, Sa_bank=2, Sa_addr=16, sz="64"), {}, dict(UR8=1, UR9=2),
          memory=[cbank(2,16,0x200000001,size=8)])
    for ura,regs,at in ((2,{"UR2":0x100},0x110),("URZ",{},16)):
        c.add("ldcu_const_RCR_", dict(URd=8, URa=ura, Sa_bank=2, Sa_offset=16, sz="64"), regs,
              dict(UR8=1, UR9=2), memory=[cbank(2,at,0x200000001,size=8)])
    c.add("ldcu_const_RCR_", dict(URd=8, URa="URZ", Sa_bank=2, Sa_offset=16, texunpack="TEXUNPACK"), {}, dict(error="sass_opaque_LDCU"))


SHARED, LOCAL = (0x7f00_59000000, 1 << 24), (0x7f00_57000000, 1 << 24)   # (base, size), as on the SM89 GPU
WINDOWS = dict(SR_SWINHI=SHARED[0] >> 32, SR_SWINLO=SHARED[0] & 0xffffffff, SR_SWINSZ=SHARED[1],
               SR_LWINHI=LOCAL[0] >> 32, SR_LWINLO=LOCAL[0] & 0xffffffff, SR_LWINSZ=LOCAL[1])
# Window edges from both sides, an address above both windows, and a low global one.
GENERIC = [SHARED[0], SHARED[0] + 0x100, sum(SHARED) - 4, sum(SHARED), LOCAL[0], LOCAL[0] + 0x40, sum(LOCAL) - 4, LOCAL[0] - 4,
           0x7fff_00000000, 0x100000]


def generic(c):
    """LD/ST route a generic address through the windows the SR_*WIN* registers describe; a decoy at
    the raw address in global memory and at the offset in the other window must not be touched."""
    def decoys(space, at, addr, n):
        out = [(s, a) for s, a in (("ram", addr), ("shared", at), ("localmem", at)) if (s, a) != (space, at)]
        return [dict(space=s, address=a, hex="ee" * n) for s, a in out]
    sizes = [("U8", "80", 0x80), ("S8", "80", 0xffffff80), ("U16", "3480", 0x8034), ("S16", "3480", 0xffff8034), ("32", "78563412", 0x12345678),
             ("64", "0100000002000000", 1), ("128", "01000000020000000300000004000000", 1)]
    for addr in GENERIC:
        space, at = memory.generic(addr, SHARED, LOCAL)
        for sz, data, want in sizes if addr in (SHARED[0] + 0x100, LOCAL[0] + 0x40, 0x100000) else sizes[4:5]:
            n = len(data) // 2
            expected = {"R8": want, **({"R9": 2} if sz in ("64", "128") else {}), **(dict(R10=3, R11=4) if sz == "128" else {})}
            base = addr + 4
            c.add("ld__sImmOffset", dict(Rd=8, Ra=2, Ra_offset=-4, sz=sz, e="E"), dict(WINDOWS, R2=base & 0xffffffff, R3=base >> 32), expected,
                  memory=[dict(space=space, address=at, hex=data), *decoys(space, at, addr, n)])
        for target in ("ram", "shared", "localmem"):
            observed = at if target != "ram" else addr
            c.add("st__sImmOffset", dict(Ra=2, Ra_offset=0, Rb=4, sz="32", e="E"), dict(WINDOWS, R2=addr & 0xffffffff, R3=addr >> 32, R4=0x12345678),
                  dict(__memory="78563412" if (target, observed) == (space, at) else "00000000"),
                  observe_memory=[dict(space=target, address=observed, size=4)])
    # Uniform forms: Ra + URb, and URb alone.
    addr = SHARED[0] + 0x200
    c.add("ld_uniform__Ra64", dict(Rd=8, Ra=2, input_reg_sz_64_dist="64", Ra_URb=4, Ra_offset=8, sz="32", e="E"),
          dict(WINDOWS, R2=0x100, R3=0, UR4=(addr - 0x108) & 0xffffffff, UR5=(addr - 0x108) >> 32), dict(R8=0x12345678),
          memory=[dict(space="shared", address=0x200, hex="78563412")])
    c.add("st_uniform__RaRZ", dict(Ra="RZ", Ra_URc=4, Ra_offset=0, Rb=6, sz="U16", e="E"),
          dict(WINDOWS, UR4=(LOCAL[0] + 0x10) & 0xffffffff, UR5=LOCAL[0] >> 32, R6=0xabcd1234), dict(__memory="3412"),
          observe_memory=[dict(space="localmem", address=0x10, size=2)])
    c.add("ld__sImmOffset", dict(Rd=8, Ra=2, Ra_offset=0, sz="32", e="noe"), dict(WINDOWS, R2=0x100), dict(error="sass_opaque_LD"))
    # An unsigned offset with its top bit set is a zero-extended absolute address.
    for op, space in (("ld", "ram"), ("ldg", "ram"), ("lds", "shared")):
        if k := c.classes.get(op + "__uImmOffset"):
            at = 1 << int(k.operand_types["Ra_offset"].args.split("/")[0]) - 1 | 0x10
            c.add(k.name, dict(Rd=8, Ra="RZ", Ra_offset=at, sz="32", **({} if op == "lds" else dict(e="E"))), WINDOWS, dict(R8=0x12345678),
                  memory=[dict(space=space, address=at, hex="78563412")])


# createpolicy's fractional evict_last (1.0) and evict_first (0.25), bit-59-clear random values, all but bit 59, and
# range forms (bit 59).
DESCRIPTORS = [0, 0x14f0000000000000, 0x1230000000000000, 0xf2a74de452e6b438, 0x6513270e269e0d37, 0xf7ffffffffffffff, 0x1d00002000000000, 0x0800000000000000]


def descriptors(c):
    """desc[URx] forms access memory as their implicit forms do whatever the descriptor, except that a
    range-form descriptor stops at the runtime; SM89 reads URx even with noexp_desc."""
    for desc in DESCRIPTORS:
        neutral = memory.descriptor_neutral(desc)
        for op, space, ra in (("ldg", "ram", 64), ("ld", "shared", 64), ("ldl", "localmem", 32)):
            cls = op + ("_memdesc__Ra64" if ra == 64 else "_memdesc_")
            if not (k := c.classes.get(cls)): continue
            for exp in ("EXP", "noexp_desc") if "e_desc" in k.operand_types else ("EXP",):
                addr = SHARED[0] + 0x100 if space == "shared" else 0x100
                regs = dict(WINDOWS, R2=(addr + 4) & 0xffffffff, R3=addr >> 32, UR8=desc & 0xffffffff, UR9=desc >> 32)
                fields = dict(Rd=8, Ra=2, Ra_URb=8, Ra_offset=-4, sz="32", **({"e_desc": exp} if "e_desc" in k.operand_types else {}),
                              **({} if ra == 32 else dict(e="E", input_reg_sz_64_dist="64")))
                want = dict(R8=0x12345678) if neutral else dict(error="range-form memory descriptor")
                c.add(cls, fields, regs, want, memory=[dict(space=space, address=0x100, hex="78563412")])
        for op, space, ra in (("stg", "ram", 64), ("st", "localmem", 64), ("stl", "localmem", 32)):
            cls = op + ("_memdesc__Ra64" if ra == 64 else "_memdesc_")
            if not (k := c.classes.get(cls)): continue
            addr = LOCAL[0] + 0x40 if op == "st" else 0x40
            regs = dict(WINDOWS, R2=addr & 0xffffffff, R3=addr >> 32, R6=0x12345678, UR8=desc & 0xffffffff, UR9=desc >> 32)
            fields = dict(Ra=2, Ra_URc=8, Ra_offset=0, Rb=6, sz="32", **({"e_desc": "EXP"} if "e_desc" in k.operand_types else {}),
                          **({} if ra == 32 else dict(e="E", input_reg_sz_64_dist="64")))
            c.add(cls, fields, regs, dict(__memory="78563412") if neutral else dict(error="range-form memory descriptor"),
                  observe_memory=[dict(space=space, address=0x40, size=4)])


def zero_fill(c):
    """A load's Pnz input: false (after its `!`) gives 0, true the loaded value; PT with `!` is always false. Pnz
    itself is written back unchanged."""
    for cls, space, addr, extra in (("ldg__sImmOffset", "ram", 0x100, {}), ("ldg_memdesc__Ra64", "ram", 0x100, dict(e_desc="noexp_desc")),
                                    ("ld__sImmOffset", "shared", SHARED[0] + 0x100, {})):
        if not (k := c.classes.get(cls)) or "Pnz" not in k.operand_types: continue
        for p, negate in ((0, 0), (0, 1), ("PT", 1)):
            for value in (0, 1):
                pnz = (value if p == 0 else 1) ^ negate
                regs = dict(WINDOWS, R2=(addr + 4) & 0xffffffff, R3=addr >> 32, P0=value)
                fields = dict(Rd=8, Ra=2, Ra_offset=-4, sz="32", e="E", Pnz=p, **{"Pnz@not": negate}, **{n: v for n, v in extra.items() if n in k.operand_types},
                              **({"input_reg_sz_64_dist": "64"} if "input_reg_sz_64_dist" in k.operand_types else {}))
                c.add(cls, fields, regs, dict(R8=memory.zero_filled(0x12345678, pnz), P0=value), memory=[dict(space=space, address=0x100, hex="78563412")])


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
        c.add(k.name, dict(fields, Rd="RZ", space="G"), {"R2": 0x100000}, dict(error="address_spaces context required"))
        if "D" in c.enum(k, "space"):
            c.add(k.name, dict(fields, Rd="RZ", space="D"), {"R2": 0x620}, {"P1": 1}, context=context)
            c.add(k.name, dict(fields, Rd="RZ", space="D"), {"R2": 0x420}, {"P1": 0}, context=context)
            c.add(k.name, dict(fields, Rd=8, space="D"), {"R2": 0x620}, dict(error="sass_opaque_QSPC"), context=context)
        if k := c.classes.get("qspc_urb__Ra32"):
            e = k.operand_types["e"]; e = "noe" if "noe" in c.enum(k, "e") else e.default
            c.add(k.name, dict(Pg="PT", Pu=1, Rd="RZ", e=e, space="S", Ra=2, input_reg_sz_32_dist="U32", Ra_URb=4, Ra_offset=0),
                  {"R2": 0x100, "UR4": 0x120}, {"P1": 1}, context=context)

    # Hardware-representable generic pointers use a 64-bit base. The adapter binds
    # these model addresses to real global/shared/local allocations/window bases.
    if k := c.classes.get('qspc__RaNonRZ'):
        physical = {'address_spaces': dict(context['address_spaces'])}
        if c.arch == 'SM90':
            physical['address_spaces']['shared_cluster'] = [[0x200, 0x300], [0x600, 0x700]]
        for origin, base in (('global', 0x100020), ('shared', 0x220), ('local', 0x420)):
            for query in ('G', 'S', 'L'):
                query_space = {'G':'global', 'S':'shared', 'L':'local'}[query]
                predicate, tag = memory.query(base, physical['address_spaces'], query_space)
                c.add(k.name, dict(Pg='PT', Pu=1, Rd=8, e='E', Ra=2, Ra_offset=0, space=query),
                      dict(R2=base, R3=0), dict(P1=predicate, R8=tag),
                      context=physical, hardware={'adapter': 'qspc', 'space': origin})


@retained("LDG predicate-output forms and aligned memory/constant register pairs are partially native; window edges/decoys, descriptors, zero-fill guards, masked/cluster queries, physical query anchors and error cases exceed generated ordinary profiles.")
def emulator_cases(c):
    loads_stores(c); generic(c); descriptors(c); zero_fill(c); qspc(c)

    from sass.decode import pydecode
    from tests.harness.case import encode
    for index, case in enumerate(list(c.complete())):
        hw = case.hardware or {'adapter': 'memory'}
        if case.cls.startswith('qspc') and not case.hardware:
            hw = {'skip': 'synthetic 32-bit address-provenance map; paired E cases use physical addresses'}
        c[index] = case._replace(hardware=hw)
        if 'word' not in case.request or 'error' in case.expected: continue
        d = pydecode.decode(c.arch, bytes.fromhex(case.request['word']))
        if d.klass.mnemonic in ('LDG', 'STG') and d.env.get('e') == 0:
            fields = {n: d.env[n] for n in d.klass.operand_types if n in d.env}
            fields.update({'@'.join(n): value for n, value in d.env.items() if isinstance(n, tuple)})
            encoded = encode(c.arch, case.cls, **dict(fields, e='E'))
            c.append(case._replace(request=dict(case.request, word=encoded), hardware={'adapter': 'memory'}))


def pnz_fixture(arch="SM89"):
    """sem_pnz with its guarded LD always issued: the branch around it never taken (@!PT) and its
    predicate, inverted, as LD's Pnz."""
    from dataclasses import replace
    from tests.harness.case import encode
    from tests.harness import scalar
    from tests.harness.gpu import Template
    f = scalar.launch("pnz", memory.pnz_kernel, arch=arch)
    t = Template(f.image, "sem_pnz")
    code = {o: t.decoded(o) for o in range(0, len(t.code), 16)}
    skip, = [d for d in code.values() if d.klass.mnemonic == "BRA" and d.env["Pg"] != 7]
    word = lambda d, **fields: (int.from_bytes(bytes.fromhex(encode(arch, d.klass.name, **{**scalar.operands(d), **fields})), "little"), t.control(o))
    words = {}
    for o, d in code.items():
        match d.klass.mnemonic:
            case "LD": words[o] = word(d, Pnz=skip.env["Pg"], **{"Pnz@not": 1 - skip.env.get(("Pg", "not"), 0)})
            case "BRA" if d is skip: words[o] = word(d, Pg=7, **{"Pg@not": 1})
    return replace(f, image=t.patch(words))


def desc_param_fixtures(arch="SM89"):
    """sem_desc_param once per bit-59-clear descriptor in DESCRIPTORS (range forms may trap)."""
    from dataclasses import replace
    from tests.harness import scalar
    f = scalar.launch("desc_param", memory.desc_param_kernel, arch=arch)
    return [replace(f, name=f"desc_param_{d:x}", arguments=((8, d),)) for d in DESCRIPTORS if memory.descriptor_neutral(d)]


def gpu_fixtures(arch="SM89"):
    from tests.harness import scalar
    return [scalar.launch("shared", lambda a, *_: a, arch=arch), scalar.launch("local", lambda a, b, c: (a + (c & 255) * b) & 0xffffffff, arch=arch),
            scalar.launch("ldc", lambda a, *_: (a & 31) ** 2, arch=arch), scalar.launch("generic", memory.generic_kernel, arch=arch),
            scalar.launch("desc", memory.desc_kernel, arch=arch), pnz_fixture(arch=arch), *desc_param_fixtures(arch=arch)]
