from tests.harness import autocase_profiles
from tests.harness.case import pair
from tests.refs import atomic

OPS = {"ATOMG", "RED"}
FAMILIES = dict.fromkeys(OPS, "atomic")
INTEGER_SIZES = {"32": (32, False), "S32": (32, True), "64": (64, False), "S64": (64, True),
                 "128": (128, False)}
FLOAT_SIZES = {"F32.FTZ.RN": 32, "F64.RN": 64, "F16x2.RN": 32}


def generate(c, k, form, profiles):
    from tests.harness.autocases import Inputs, Unsupported
    if profiles != ["atomic_forms"]: raise Unsupported("incompatible atomic profile")
    size = INTEGER_SIZES.get(form.get("sz"))
    op = "CAS" if "cas" in k.operand_types else form.get("op")
    floating = form.get("sz") in FLOAT_SIZES and op == "ADD"
    reduction = k.mnemonic == "RED"
    if reduction and (k.name != "red_uniform__memdesc" or op != "ADD" or form.get("sz") != "32"):
        raise Unsupported("RED form unresolved")
    if floating:
        size = FLOAT_SIZES[form["sz"]], False
    supported = {"ADD", "MIN", "MAX", "INC", "DEC", "AND", "OR", "XOR", "EXCH", "CAS"}
    if c.discovery: supported.add("SAFEADD")
    if size is None or op not in supported:
        raise Unsupported("atomic operation/type semantics unresolved")
    if form.get("sem") == "MMIO": raise Unsupported("atomic MMIO environment unresolved")
    width, signed = size
    mask = (1 << width) - 1
    edges = autocase_profiles.PROFILES["atomic_float_forms"][form["sz"]] if floating else autocase_profiles.PROFILES["atomic_forms"]["values"]
    values = tuple((old & mask, value & mask, replacement & mask) for old, value, replacement in edges)
    for j, (old, value, replacement) in enumerate(values):
        for dest in (None,) if reduction else (8, 4, 12, 255):
            s = Inputs(c, k, form)
            s.fields.update(Ra=4, Rb=12)
            if not reduction: s.fields.update(Rd=dest, Pu=1)
            if k.operand_types["Ra"].type == "ZeroRegister": s.fields["Ra"] = "RZ"
            offset = -16 if k.operand_types["Ra_offset"].type == "SImm" else 16
            uniform = "Ra_URc" in k.operand_types and "memoryDescriptor" not in k.operand_types
            address = 0x100
            base = address - offset - (0x40 if uniform else 0)
            if s.fields["Ra"] == "RZ":
                offset = address - (0x40 if uniform else 0)
            else:
                s.registers.update(pair("R4", base) if form.get("e") == "E" else {"R4": base})
            s.fields["Ra_offset"] = offset
            if "Ra_URc" in k.operand_types:
                s.fields["Ra_URc"] = 6
                s.registers.update(pair("UR6", 0x40 if uniform else 0))
            s.registers.update({f"R{12 + n}": value >> (32 * n) & 0xffffffff for n in range(width // 32)})
            if "Rc" in k.operand_types:
                s.fields["Rc"] = 16
                s.registers.update({f"R{16 + n}": replacement >> (32 * n) & 0xffffffff for n in range(width // 32)})
            if not reduction: s.registers["P1"] = j % 2
            s.memory.append(dict(space="ram", address=address, hex=old.to_bytes(width // 8, "little").hex()))
            expected = {} if reduction else {f"R{dest + n}": old >> (32 * n) & 0xffffffff for n in range(width // 32)} if dest != 255 else {"R255": 0}
            if not c.discovery:
                result = atomic.float_add(form["sz"], old, value) if floating else atomic.update(op, old, value, width, signed, replacement)
                effects = ["sass_memory_order"] + (["sass_memory_descriptor"] if "memoryDescriptor" in k.operand_types else []) + [
                    "sass_ordered_atomic_enter", "sass_ordered_atomic_exit"]
                if not reduction: expected["P1"] = 0
                expected.update(__memory=result.to_bytes(width // 8, "little").hex(),
                                __max_ops=dict(count=2048 if floating else 128, userops=effects, branches=True))
            s.add(expected, form, observe=[*expected, *([] if reduction else ["P1"])] if c.discovery else None,
                  observe_memory=[dict(space="ram", address=address, size=width // 8)],
                  hardware={"adapter": "memory"})
    return c
