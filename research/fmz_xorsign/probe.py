"""SM89 probes of FMUL/FFMA.FMZ and FMNMX.XORSIGN with source modifiers (2026-10-20).

    uv run python -m research.fmz_xorsign.probe OUT.json   # GPU, outside the sandbox
    uv run python -m research.fmz_xorsign.probe OUT.json --check

--check compares saved observations with tests.refs.float (fmz, fmnmx_xorsign)."""
import itertools, json, random, sys
from tests.refs.float import fmnmx_xorsign, fmz, source_modifiers

EDGES = [0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x00800000, 0x80800000, 0x3f800000, 0xbf800000, 0x7f800000, 0xff800000,
         0x7fc00000, 0xffc00001, 0x7f7fffff, 0x3fc00001, 0x00c00000, 0x3f000000, 0x40400000]


def modifiers(names):
    return [dict(zip([f"{n}@{m}" for n in names for m in ("absolute", "negate")], bits)) for bits in itertools.product((0, 1), repeat=2 * len(names))]


def requests():
    rng = random.Random(7)
    seeded = [rng.getrandbits(32) for _ in range(6)]
    values = EDGES + seeded
    pairs = list(itertools.product(values, values))[::3]
    for rnd, sat, scale in itertools.product(("RN", "RM", "RP", "RZ"), ("SAT", "nosat"), ("noscale", "D2", "D8", "M2", "M8")):
        for a, b in pairs: yield "fmul__RRR_RR", dict(Rd=8, Ra=2, Rb=4, fmz="FMZ", rnd=rnd, sat=sat, scale=scale), dict(R2=a, R4=b)
    for m in modifiers(("Ra", "Rb")):
        for a, b in pairs[::2]: yield "fmul__RRR_RR", dict(Rd=8, Ra=2, Rb=4, fmz="FMZ", **m), dict(R2=a, R4=b)
    for a, b in pairs[::4]: yield "fmul__RIR_RI", dict(Rd=8, Ra=2, Sb=b, fmz="FMZ"), dict(R2=a)
    addends = [0, 0x80000000, 1, 0x80000001, 0x3f800000, 0xbf800000, 0x7f800000, 0xff800000, 0x7fc00000, 0xffc00001, 0x00800000, 0x80800000, seeded[0]]
    factors = [0, 0x80000000, 1, 0x7f800000, 0x7fc00000, 0x3f800000, 0xbf800000, 0x00800000, 0x3f000000, seeded[1]]
    for rnd, sat in itertools.product(("RN", "RM", "RP", "RZ"), ("SAT", "nosat")):
        for a, b, c in itertools.product(factors, factors, addends):
            yield "ffma__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmz="FMZ", rnd=rnd, sat=sat), dict(R2=a, R4=b, R6=c)
    triples = [(rng.choice(values), rng.choice(values), rng.choice(values)) for _ in range(40)]
    for m in modifiers(("Ra", "Rb", "Rc")):
        for a, b, c in triples: yield "ffma__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Rc=6, fmz="FMZ", rnd="RM", **m), dict(R2=a, R4=b, R6=c)
    for m, nan, ftz, inverted in itertools.product(modifiers(("Ra", "Rb")), ("NAN", "nonan"), ("FTZ", "noftz"), (0, 1)):
        for a, b in pairs[::2]:
            yield "fmnmx__RRR_RRR", dict(Rd=8, Ra=2, Rb=4, Pp="PT", xorsign="XORSIGN", nan=nan, ftz=ftz, **{"Pp@not": inverted}, **m), dict(R2=a, R4=b)


def reference(cls, fields, registers):
    encoded = [registers["R2"], fields.get("Sb", registers.get("R4")), registers.get("R6")]
    xs = [source_modifiers(v, 32, bool(fields.get(n + "@absolute")), bool(fields.get(n + "@negate"))) if v is not None else None
          for n, v in zip(("Ra", "Rb", "Rc"), encoded)]
    if cls.startswith("fmnmx"):
        return fmnmx_xorsign(*xs[:2], not fields["Pp@not"], fields["nan"] == "NAN", fields["ftz"] == "FTZ", encoded=encoded[:2])
    scale = {"D8": -3, "D2": -1, "M2": 1, "M8": 3}.get(fields.get("scale"), 0)
    op = "mul" if cls.startswith("fmul") else "fma"
    return fmz(op, xs[:2] if op == "mul" else xs, fields.get("rnd", "RN"), fields.get("sat") == "SAT", scale)


def main(out, check=False):
    if check:
        saved = json.load(open(out))
        wrong = [(o["instruction"], meta[2], o["registers"]["R8"]) for meta, o in zip(saved["requests"], saved["observations"])
                 if o["registers"]["R8"] != reference(*meta)]
        print(f"{len(wrong)} of {len(saved['requests'])} observations differ from the references", *wrong[:10], sep="\n")
        return
    from tests.harness.case import Cases
    from tests.harness import device_cases
    c, seen, made = Cases("SM89"), set(), []
    for cls, fields, registers in requests():
        case = c.probe(cls, fields, registers, observe=["R8"])
        if case.id in seen: c.pop(); continue
        seen.add(case.id); made.append((cls, fields, registers))
    result = device_cases.observe(c)
    json.dump(dict(requests=made, **result), open(out, "w"))


if __name__ == "__main__":
    main(sys.argv[1], "--check" in sys.argv)
