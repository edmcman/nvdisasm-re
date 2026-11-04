from tests.harness import autocase_profiles
from tests.refs import matrix

OPS = {"LDSM", "HMMA"}
FAMILIES = {op: "matrix" for op in OPS}


def generate(c, k, form, profiles):
    from tests.harness.autocases import Inputs, Unsupported
    if profiles != ["matrix_forms"]: raise Unsupported("incompatible matrix profile")
    if k.mnemonic == "HMMA": return hmma(c, k, form)
    if form.get("sz") != "16" or form.get("mode") not in ("M88", "MT88"):
        raise Unsupported("packed/expanded matrix load mapping unresolved")
    count = int(form["num"])
    addresses = [0x100 + lane * 16 for lane in range(32)]
    uniform = "Ra_URb" in k.operand_types
    for lane in autocase_profiles.PROFILES["matrix_forms"]["lanes"]:
        for dest in (8, 4):
            s = Inputs(c, k, form)
            s.fields.update(Ra=4, Rd=dest, Ra_offset=-16)
            groups = dict(Ra=[[address + 16 - (32 if uniform else 0)] for address in addresses])
            if k.operand_types["Ra"].type == "ZeroRegister":
                s.fields["Ra"] = "RZ"
                raise Unsupported("matrix load absolute row address cannot distinguish rows")
            if uniform:
                s.fields["Ra_URb"] = 6
                groups["Ra_URb"] = [[32] for _ in range(32)]
                s.registers["UR6"] = 32
            s.registers["R4"] = groups["Ra"][lane][0]
            data = b"".join((row * 8 + column + 1 + matrix_id * 0x100).to_bytes(2, "little")
                            for matrix_id in range(4) for row in range(8) for column in range(8))
            s.memory.append(dict(space="shared", address=0x100, hex=data.hex()))
            cells = dict(zip(range(0x100, 0x100 + len(data)), data))
            result = matrix.load(cells, addresses, lane, count, form["mode"] == "MT88")
            s.add({f"R{dest + j}": word for j, word in enumerate(result)}, form,
                  context={"warp": dict(lane=lane, active=0xffffffff, operand_groups=groups)},
                  hardware={"adapter": "matrix"})
    return c


def hmma(c, k, form):
    import random
    import struct
    from tests.harness.autocases import Inputs, Unsupported
    if k.name != "hmma_x8_" or (form.get("size"), form.get("srcfmt"), form.get("dstfmt")) != ("16816", "F16", "F32"):
        raise Unsupported("HMMA adapter covers direct 16816 F16/F32")
    if any(form.get(name + "@negate") == "set" for name in ("Ra", "Rb")) or form.get("UPp@not") == "set":
        raise Unsupported("HMMA negate/accumulator predicate unresolved")
    bits = lambda value, fmt: int.from_bytes(struct.pack(fmt, value), "little")
    for pattern in autocase_profiles.PROFILES["hmma_forms"]["patterns"]:
        rng = random.Random(int(pattern.split("_")[-1])) if pattern.startswith("random_") else None
        def finite(width, exponent_mask):
            value = rng.getrandbits(width)
            return value ^ (1 << (width - 2)) if value & exponent_mask == exponent_mask else value
        if rng:
            random_a = [[struct.unpack("<e", finite(16, 0x7c00).to_bytes(2, "little"))[0]
                         for _ in range(16)] for _ in range(16)]
            random_b = [[struct.unpack("<e", finite(16, 0x7c00).to_bytes(2, "little"))[0]
                         for _ in range(8)] for _ in range(16)]
            random_c = [[struct.unpack("<f", finite(32, 0x7f800000).to_bytes(4, "little"))[0]
                         if int(pattern.split("_")[-1]) % 2 else rng.uniform(-4, 4)
                         for _ in range(8)] for _ in range(16)]
        def a(row, column):
            match pattern:
                case name if name.startswith("random_"): return random_a[row][column]
                case "identity": return int(row == column)
                case "accumulator": return 0
                case "coordinates": return row + 1
                case "nan_a": return float("nan")
                case "subnormal_c": return 0
                case "negative_zero": return -0.0
                case "zero_times_inf": return 0
                case "max_half": return 65504
                case "min_half" | "half_subnormals": return 2 ** -24
                case "quarter_products": return 0.5
                case "negative_quarters": return -0.5
                case "unnormalized_product": return 1.5 if column == 0 else 1
                case name if name.startswith("round_"): return float(name.split("_")[1])
                case _: return 1
        def b(row, column):
            match pattern:
                case name if name.startswith("random_"): return random_b[row][column]
                case "coordinates": return column + 1
                case "identity": return row + 1
                case "small": return 2 ** -24
                case "cancel": return 1 if row < 8 else -1
                case "cancel_even": return 1 if row % 2 == 0 else -1
                case "nan_b": return float("nan")
                case "infinity_cancel": return float("inf") if row < 8 else -float("inf")
                case "zero_times_inf" | "positive_inf": return float("inf")
                case "negative_inf": return -float("inf")
                case "max_half": return 65504
                case "half_subnormals": return 2 ** -24
                case "quarter_products" | "negative_quarters": return 2 ** -24
                case "unnormalized_product": return 1.5 if row == 0 else 2 ** -24 if row <= 4 else 0
                case "normalized_product": return 2.25 if row == 0 else 2 ** -24 if row <= 4 else 0
                case name if name.startswith("round_"): return 2 ** -24 if row == 0 else 0
                case name if name.startswith("pair_"):
                    return 2 ** -24 if row in map(int, name.split("_")[1:]) else 0
                case name if name.startswith("cancel_pair_"):
                    return 1 if row == 0 else -1 if row == int(name.split("_")[-1]) else 0
                case _: return 1
        def accumulator(row, column):
            match pattern:
                case name if name.startswith("random_"): return random_c[row][column]
                case "accumulator": return row * 8 + column + 1
                case "small": return 1
                case "quarter_products" | "negative_quarters": return 1
                case "cancel": return 2 ** -24
                case name if name.startswith("pair_"): return 1
                case name if name.startswith("cancel"): return 2 ** -24
                case name if name.startswith("round_"): return 1 if name.endswith("even") else 1 + 2 ** -23
                case "nan_c": return float("nan")
                case "subnormal_c": return 2 ** -149
                case "negative_zero": return -0.0
                case _: return 0
        groups = dict(Ra=[], Rb=[], Rc=[])
        for lane in range(32):
            row, thread = lane // 4, lane % 4
            half_a = [bits(a(row + dr, thread * 2 + dk + n), "<e")
                      for dk, dr in ((0, 0), (0, 8), (8, 0), (8, 8)) for n in (0, 1)]
            half_b = [bits(b(thread * 2 + dk + n, row), "<e") for dk in (0, 8) for n in (0, 1)]
            if pattern == "snan_a": half_a = [0x7c01] * 8
            if pattern == "snan_b": half_b = [0xfc01] * 4
            groups["Ra"].append([half_a[j] | half_a[j + 1] << 16 for j in range(0, 8, 2)])
            groups["Rb"].append([half_b[j] | half_b[j + 1] << 16 for j in range(0, 4, 2)])
            groups["Rc"].append([bits(accumulator(row + dr, thread * 2 + n), "<f") for dr in (0, 8) for n in (0, 1)])
            if pattern == "snan_c": groups["Rc"][-1] = [0xff800001] * 4
        lanes = range(32) if rng else autocase_profiles.PROFILES["matrix_forms"]["lanes"] if pattern in (
            "ones", "coordinates", "identity", "accumulator", "small", "cancel") else (0,)
        for lane in lanes:
            placements = ((40, None), (16, None), (24, None), (32, None), (40, 0), (40, 1)) if pattern == "ones" and lane == 0 else ((40, None),)
            for destination, disabled in placements:
                s = Inputs(c, k, form)
                s.fields.update(Ra=16, Rb=24, Rc=32, Rd=destination)
                for name, register in (("Ra", 16), ("Rb", 24), ("Rc", 32)):
                    s.registers.update({f"R{register + j}": value for j, value in enumerate(groups[name][lane])})
                if disabled is not None:
                    s.fields.update(Pg=0, **{"Pg@not": disabled})
                    s.registers["P0"] = disabled
                    s.registers.update({f"R{destination + j}": 0x12345678 + j for j in range(4)})
                values = [s.registers[f"R{destination + j}"] for j in range(4)] if disabled is not None else matrix.hmma(groups, lane, c.arch) if not c.discovery else []
                expected = {f"R{destination + j}": value for j, value in enumerate(values)}
                floor = f"hmma-{pattern}-{destination}-{disabled}"
                s.add(expected, form, observe=[f"R{destination + j}" for j in range(4)],
                      context={"warp": dict(lane=lane, active=0xffffffff, operand_groups=groups)},
                      hardware={"adapter": "matrix", "pattern": pattern, "sampling_floor": floor})
    return c
