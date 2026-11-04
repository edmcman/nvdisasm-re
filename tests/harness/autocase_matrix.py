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


def hmma_cases(c, k, form, groups, pattern, lanes, source="F16", count=16):
    from tests.harness.autocases import Inputs
    words = 2 if form.get("dstfmt") == "F16" else 4
    predicates = (("UPT", None), (0, 0), (0, 1), (6, 0), (6, 1)) if "UPp" in k.operand_types and pattern in (
        "accumulator", "nan_c", "snan_c", "raw_00000000_00000000_ff800001") else (("UPT", None),)
    for lane in lanes:
        placements = ((40, None, None), (16, None, None), (24, None, None), (32, None, None),
                      (40, 0, None), (40, 1, None), ("RZ", None, None),
                      *((40, None, name) for name in ("Ra", "Rb", "Rc"))) if pattern == "ones" and lane == 0 else ((40, None, None),)
        variants = [(*placement, {}) for placement in placements]
        if pattern in ("coordinates", "identity") and lane == 0:
            variants += [(40, None, None, alias) for alias in (
                {"Rb": 16}, {"Rb": 18}, {"Rc": 16}, {"Rc": 24},
                {"Ra": 32}, {"Rb": 32}, {"Rb": 16, "Rc": 16})]
        for destination, disabled, zero, aliases in variants:
            for predicate, value in predicates:
                s = Inputs(c, k, form)
                s.fields.update(Ra=16, Rb=24, Rc=32, Rd=destination)
                s.fields.update(aliases)
                inputs = groups | {zero: [[0] * len(words) for words in groups[zero]]} if zero else groups
                if zero: s.fields[zero] = "RZ"
                if aliases:
                    physical = [{register + j: v for name, register in (("Ra", 16), ("Rb", 24), ("Rc", 32))
                                 for j, v in enumerate(groups[name][owner])} for owner in range(32)]
                    inputs = {name: [[physical[owner].get(s.fields[name] + j, 0) for j in range(len(words))]
                                     for owner, words in enumerate(groups[name])] for name in groups}
                if "UPp" in k.operand_types:
                    s.fields["UPp"] = predicate
                    if value is not None: s.registers[f"UP{predicate}"] = value
                for name, register in (("Ra", 16), ("Rb", 24), ("Rc", 32)):
                    s.registers.update({f"R{register + j}": v for j, v in enumerate(groups[name][lane])})
                if disabled is not None:
                    s.fields.update(Pg=0, **{"Pg@not": disabled})
                    s.registers["P0"] = disabled
                    s.registers.update({f"R{destination + j}": 0x12345678 + j for j in range(words)})
                enabled = bool(1 if value is None else value) ^ (form.get("UPp@not") == "set")
                values = [] if c.discovery or destination == "RZ" else [s.registers[f"R{destination + j}"] for j in range(words)] if disabled is not None else matrix.hmma(
                    inputs, lane, c.arch, source, count, enabled,
                    tuple(form.get(name + "@negate") == "set" for name in ("Ra", "Rb")), form["dstfmt"])
                names = list(s.registers) if destination == "RZ" else [f"R{destination + j}" for j in range(words)]
                expected = {name: s.registers[name] for name in names} if destination == "RZ" and not c.discovery else dict(zip(names, values))
                s.add(expected, form, observe=names,
                      context={"warp": dict(lane=lane, active=0xffffffff, operand_groups=inputs)},
                      hardware={"adapter": "matrix", "pattern": pattern,
                                "sampling_floor": f"hmma-{source}-{pattern}-{destination}-{disabled}-{zero}-{predicate}-{value}"
                                + (f"-{sorted(aliases.items())}" if aliases else "")})


def hmma(c, k, form):
    import random
    import struct
    from tests.harness.autocases import Unsupported
    if form.get("srcfmt") == "TF32": return tf32(c, k, form)
    source = form.get("srcfmt")
    half_output = form.get("dstfmt") == "F16"
    if k.name != "hmma_x8_" or form.get("size") not in ("1688", "16816") or (
        source, form.get("dstfmt")) not in (("F16", "F32"), ("BF16", "F32"), ("F16", "F16")):
        raise Unsupported("HMMA adapter covers direct 1688/16816 F16/F16, F16/F32 and BF16/F32")
    count = int(form["size"][3:])
    def bits(value, fmt):
        return int.from_bytes(struct.pack("<f", value), "little") >> 16 if source == "BF16" and fmt == "<e" else int.from_bytes(struct.pack(fmt, value), "little")
    profile = "hmma_f16_output_forms" if half_output else "hmma_bf16_forms" if source == "BF16" else "hmma_forms"
    midpoint = 2 ** (-11 if half_output else -24)
    for pattern in autocase_profiles.PROFILES[profile]["patterns"]:
        match pattern.split("_"):
            case ["pair", first, last] if max(int(first), int(last)) >= count: continue
            case ["cancel", "pair", index] if int(index) >= count: continue
        rng = random.Random(int(pattern.split("_")[-1])) if pattern.startswith("random_") else None
        def finite(width, exponent_mask):
            value = rng.getrandbits(width)
            return value ^ (1 << (width - 2)) if value & exponent_mask == exponent_mask else value
        if rng:
            unpack = lambda v: struct.unpack("<f", (v << 16).to_bytes(4, "little"))[0] if source == "BF16" else struct.unpack("<e", v.to_bytes(2, "little"))[0]
            mask = 0x7f80 if source == "BF16" else 0x7c00
            random_a = [[unpack(finite(16, mask))
                         for _ in range(count)] for _ in range(16)]
            random_b = [[unpack(finite(16, mask))
                         for _ in range(8)] for _ in range(count)]
            random_c = [[struct.unpack("<e", finite(16, 0x7c00).to_bytes(2, "little"))[0] if half_output else
                         struct.unpack("<f", finite(32, 0x7f800000).to_bytes(4, "little"))[0]
                         if int(pattern.split("_")[-1]) % 2 else rng.uniform(-4, 4)
                         for _ in range(8)] for _ in range(16)]
        def a(row, column):
            match pattern:
                case p if p.startswith("half_boundary_"): return 1 if column == 0 else 2 ** -24 if column == 1 else 0
                case p if p.startswith("tiny_precision_"):
                    q = int(p.split("_")[-1])
                    return (2 - 2 ** (1 - q)) * 2 ** -75 if column % 8 == 0 else 2 ** ((-150 - q) // 2) if column % 8 in (1, 2) else 0
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
                case p if p.startswith("half_boundary_"):
                    _, _, tail, sign, parity = p.split("_")
                    return midpoint if row == 0 else int(sign) * 2 ** (int(tail) + 24) if row == 1 else 0
                case p if p.startswith("tiny_precision_"):
                    q = int(p.split("_")[-1])
                    return 2 ** -75 if row % 8 == 0 else 2 ** (-150 - q - (-150 - q) // 2) if row % 8 in (1, 2) else 0
                case name if name.startswith("random_"): return random_b[row][column]
                case "coordinates": return column + 1
                case "identity": return row + 1
                case "small": return 2 ** -24
                case "cancel": return 1 if row < count // 2 else -1
                case "cancel_even": return 1 if row % 2 == 0 else -1
                case "nan_b": return float("nan")
                case "infinity_cancel": return float("inf") if row < count // 2 else -float("inf")
                case "zero_times_inf" | "positive_inf": return float("inf")
                case "negative_inf": return -float("inf")
                case "max_half": return 65504
                case "half_subnormals": return 2 ** -24
                case "quarter_products" | "negative_quarters": return midpoint
                case "unnormalized_product": return 1.5 if row == 0 else midpoint if row <= 4 else 0
                case "normalized_product": return 2.25 if row == 0 else midpoint if row <= 4 else 0
                case name if name.startswith("round_"): return midpoint if row == 0 else 0
                case name if name.startswith("pair_"):
                    return midpoint if row in map(int, name.split("_")[1:]) else 0
                case name if name.startswith("cancel_pair_"):
                    return 1 if row == 0 else -1 if row == int(name.split("_")[-1]) else 0
                case _: return 1
        def accumulator(row, column):
            match pattern:
                case p if p.startswith("half_boundary_"): return 1 if p.endswith("even") else 1 + midpoint * 2
                case name if name.startswith("random_"): return random_c[row][column]
                case "accumulator": return row * 8 + column + 1
                case "small": return 1
                case "quarter_products" | "negative_quarters": return 1
                case "cancel": return 2 ** -24
                case name if name.startswith("pair_"): return 1
                case name if name.startswith("cancel"): return 2 ** -24
                case name if name.startswith("round_"): return 1 if name.endswith("even") else 1 + midpoint * 2
                case "nan_c": return float("nan")
                case "subnormal_c": return 2 ** (-24 if half_output else -149)
                case "negative_zero": return -0.0
                case _: return 0
        groups = dict(Ra=[], Rb=[], Rc=[])
        for lane in range(32):
            row, thread = lane // 4, lane % 4
            half_a = [bits(a(row + dr, thread * 2 + dk + n), "<e")
                      for dk in range(0, count, 8) for dr in (0, 8) for n in (0, 1)]
            half_b = [bits(b(thread * 2 + dk + n, row), "<e") for dk in range(0, count, 8) for n in (0, 1)]
            if pattern == "snan_a": half_a = [0x7f81 if source == "BF16" else 0x7c01] * (count // 2)
            if pattern == "snan_b": half_b = [0xff81 if source == "BF16" else 0xfc01] * (count // 4)
            if pattern.startswith("raw_"):
                _, raw_a, raw_b, raw_c = pattern.split("_")
                half_a, half_b = [int(raw_a, 16)] * (count // 2), [int(raw_b, 16)] * (count // 4)
            groups["Ra"].append([half_a[j] | half_a[j + 1] << 16 for j in range(0, count // 2, 2)])
            groups["Rb"].append([half_b[j] | half_b[j + 1] << 16 for j in range(0, count // 4, 2)])
            values = [bits(accumulator(row + dr, thread * 2 + n), "<e" if half_output else "<f")
                      for dr in (0, 8) for n in (0, 1)]
            groups["Rc"].append([values[j] | values[j + 1] << 16 for j in (0, 2)] if half_output else values)
            if pattern == "snan_c": groups["Rc"][-1] = [0xfc01fc01] * 2 if half_output else [0xff800001] * 4
            if pattern.startswith("raw_"):
                value = int(raw_c, 16)
                groups["Rc"][-1] = [value | value << 16] * 2 if half_output else [value] * 4
        lanes = range(32) if rng else autocase_profiles.PROFILES["matrix_forms"]["lanes"] if pattern in (
            "ones", "coordinates", "identity", "accumulator", "small", "cancel") else (0,)
        hmma_cases(c, k, form, groups, pattern, lanes, source, count)
    return c


def tf32(c, k, form):
    import random
    import struct
    from tests.harness.autocases import Unsupported
    if k.name != "hmma_x8_" or form.get("dstfmt") != "F32" or form.get("size") not in ("1684", "1688"):
        raise Unsupported("TF32 adapter covers direct 1684/1688 F32")
    count = int(form["size"][3:])
    bits = lambda v: int.from_bytes(struct.pack("<f", v), "little")
    for pattern in autocase_profiles.PROFILES["hmma_tf32_forms"]["patterns"]:
        if pattern.startswith(("basis_", "pair_", "cancel_")) and any(int(n) >= count for n in pattern.split("_")[1:]):
            continue
        rng = random.Random(int(pattern.removeprefix("random_"))) if pattern.startswith("random_") else None
        def value(name, row, column):
            index = column if name == "Ra" else row
            match pattern:
                case "coordinates": return bits(row + 1 if name == "Ra" else column + 1) if name != "Rc" else 0
                case "accumulator": return bits(row * 8 + column + 1) if name == "Rc" else 0
                case "identity": return bits(int(row == column)) if name == "Ra" else bits(row + 1) if name == "Rb" else 0
                case p if p.startswith("tiny_power_"):
                    _, _, power, accumulator = p.split("_")
                    power = int(power)
                    return int(accumulator, 16) if name == "Rc" else bits(2 ** (power // 2 if name == "Ra" else power - power // 2))
                case "tiny_join" | "tiny_split":
                    a = (1.984375 * 2 ** -75, 2 ** -78, 2 ** -78, 2 ** -79)
                    b = (2 ** -75, 2 ** -79, 2 ** (-79 if pattern == "tiny_join" else -80),
                         0 if pattern == "tiny_join" else 2 ** -79)
                    return bits((a if name == "Ra" else b)[index % 4]) if name != "Rc" else 0
                case p if p.startswith("tiny_precision_"):
                    q = int(p.split("_")[-1])
                    if name == "Rc" or index % 4 == 3: return 0
                    if index % 4 == 0: return bits((2 - 2 ** (1 - q)) * 2 ** -75 if name == "Ra" else 2 ** -75)
                    power = -150 - q
                    return bits(2 ** (power // 2 if name == "Ra" else power - power // 2))
                case p if p.startswith("tiny_alignment_"):
                    exponent = int(p.split("_")[-1])
                    difference = max(1, 2 ** (-129 - exponent))
                    if name == "Rc" or index % 4 == 3: return 0
                    if index % 4 == 2:
                        power = exponent - 24
                        return bits((1 if name == "Ra" else -1) * 2 ** (power // 2 if name == "Ra" else power - power // 2))
                    factors = ((1 + 1 / 1024, 1 + difference / 1024),
                               (1, -(1 + (difference + 1) / 1024)))
                    power = exponent // 2 if name == "Ra" else exponent - exponent // 2
                    return bits(factors[index % 4][name == "Rb"] * 2 ** power)
                case "overflow_cancel" | "overflow_groups" | "wide_cancel":
                    if name == "Rc": return bits(1)
                    if name == "Ra": return 0x7f7fe000
                    sign = index % 2 if pattern != "overflow_groups" else int(index >= 4)
                    return (sign << 31) | (0x7f7fe000 if pattern == "wide_cancel" else 0x40000000)
                case "unnormalized":
                    if name == "Rc": return 0
                    return bits(1.5 if index == 0 else 1 if name == "Ra" else 2 ** -24)
                case p if p.startswith("basis_"):
                    return bits(int((column if name == "Ra" else row) == int(p.split("_")[1]))) if name != "Rc" else 0
                case p if p.startswith("pair_"):
                    return bits(1 if name != "Rb" else 2 ** -24) if name != "Rb" or row in map(int, p.split("_")[1:]) else 0
                case p if p.startswith("cancel_"):
                    return bits(1 if name != "Rb" or row == 0 else -1) if name != "Rb" or row in (0, int(p.split("_")[-1])) else 0
                case p if p.startswith("raw_"):
                    return int(p.split("_")[("Ra", "Rb", "Rc").index(name) + 1], 16)
                case p if p.startswith("random_"):
                    v = rng.getrandbits(32)
                    return v ^ 0x40000000 if v & 0x7f800000 == 0x7f800000 else v
                case _: return bits(1) if name != "Rc" else 0
        groups = dict(Ra=[], Rb=[], Rc=[])
        for lane in range(32):
            row, thread = divmod(lane, 4)
            groups["Ra"].append([value("Ra", row + dr, thread + dk) for dk in range(0, count, 4) for dr in (0, 8)])
            groups["Rb"].append([value("Rb", thread + dk, row) for dk in range(0, count, 4)])
            groups["Rc"].append([value("Rc", row + dr, thread * 2 + n) for dr in (0, 8) for n in (0, 1)])
        lanes = range(32) if rng else autocase_profiles.PROFILES["matrix_forms"]["lanes"] if pattern in (
            "ones", "coordinates", "identity", "accumulator") or pattern.startswith("basis_") else (0,)
        hmma_cases(c, k, form, groups, pattern, lanes, "TF32", count)
    return c
