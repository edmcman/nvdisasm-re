"""Generate runtime-input SM75 compiler searches; this does not execute GPU probes.

The compiler matrix covers conversion rounding, operand position, scalar/packed
consumers, observable broadcasts and float arithmetic before half conversion.
Keep selected informative traces under tests/ptx; keep compiled output in cache.
"""
import argparse
from pathlib import Path


HEADER = ".version 8.8\n.target sm_75\n.address_size 64\n"


def entry(name, instructions, store="st.global.b32 [dest], r15;"):
    return f"""
.visible .entry {name}(.param .u64 input, .param .u64 output) {{
    .reg .b64 a, b, offset, source, dest;
    .reg .b32 r<16>, tid;
    .reg .b16 h<16>;
    .reg .f32 f<16>;
    .reg .pred p, q;
    ld.param.u64 a, [input];
    ld.param.u64 b, [output];
    mov.u32 tid, %tid.x;
    mul.wide.u32 offset, tid, 16;
    add.u64 source, a, offset;
    mul.wide.u32 offset, tid, 4;
    add.u64 dest, b, offset;
    ld.global.v4.b32 {{r0, r1, r2, r3}}, [source];
    mov.b32 f0, r0;
    mov.b32 {{h0, h1}}, r1;
    mov.b32 {{h2, h3}}, r2;
    {instructions}
    {store}
    ret;
}}
"""


def matrix():
    for rounding in ("rn", "rz", "rm", "rp"):
        conversion = f"cvt.{rounding}.f16.f32 h4, f0;"
        yield entry(f"broadcast_{rounding}", conversion + "\nmov.b32 r15, {h4, h4};")
        for packed in (False, True):
            suffix = "packed" if packed else "scalar"
            scalar = ("h4", "h0", "h2")
            vector = ("r4", "r1", "r2")
            prefix = conversion + ("\nmov.b32 r4, {h4, h4};" if packed else "")
            for operation, arity in (("add", 2), ("mul", 2), ("fma", 3), ("set", 2), ("setp", 2)):
                for position in range(arity):
                    args = list((vector if packed else scalar)[:arity])
                    args[0], args[position] = args[position], args[0]
                    fmt = "f16x2" if packed else "f16"
                    if operation == "setp":
                        result = f"setp.lt.{fmt} {'p|q' if packed else 'p'}, {', '.join(args)};\nselp.u32 r15, 1, 0, p;"
                        if packed:
                            result += "\nselp.u32 r14, 2, 0, q;\nor.b32 r15, r15, r14;"
                    elif operation == "set":
                        result = f"set.lt.{fmt}.{fmt} {'r15' if packed else 'h15'}, {', '.join(args)};"
                    else:
                        result = f"{operation}.rn.{fmt} {'r15' if packed else 'h15'}, {', '.join(args)};"
                    if not packed and operation != "setp":
                        result += "\nmov.b32 r15, {h15, h15};"
                    yield entry(f"{operation}_{suffix}_{position}_{rounding}", prefix + "\n" + result)
        # Does keeping converted values observable inhibit, or expose, fusion?
        yield entry(f"add_broadcast_consumed_{rounding}", conversion + """
    mov.b32 r4, {h4, h4};
    add.rn.f16x2 r5, r4, r1;
    xor.b32 r15, r4, r5;""")
        for operation, arity in (("add", 2), ("mul", 2), ("fma", 3)):
            for position in range(arity):
                setup = "cvt.f32.f16 f1, h0;\ncvt.f32.f16 f2, h2;"
                args = ["f0", "f1", "f2"][:arity]
                args[0], args[position] = args[position], args[0]
                yield entry(f"float_{operation}_{position}_to_half_{rounding}", setup + f"""
    {operation}.rn.f32 f3, {', '.join(args)};
    cvt.{rounding}.f16.f32 h15, f3;
    mov.b32 r15, {{h15, h15}};""")
    for operation, arity in (("add", 2), ("mul", 2), ("fma", 3)):
        args = ["f0", "f1", "f2"][:arity]
        yield entry(f"half_{operation}_to_float", f"""
    cvt.f32.f16 f0, h0;
    cvt.f32.f16 f1, h1;
    cvt.f32.f16 f2, h2;
    {operation}.rn.f32 f3, {', '.join(args)};
    mov.b32 r15, f3;""")
        yield entry(f"{operation}_half_then_widen", f"""
    {operation}.rn.f16 h15, {', '.join(['h0','h1','h2'][:arity])};
    cvt.f32.f16 f3, h15;
    mov.b32 r15, f3;""")
    # A scalar store cannot make the optimizer keep a broadcast merely to
    # satisfy an observable high lane.
    for rounding in ("rn", "rz", "rm", "rp"):
        for operation, arity in (("add", 2), ("mul", 2), ("fma", 3)):
            for position in range(arity):
                args = ["h4", "h0", "h2"][:arity]
                args[0], args[position] = args[position], args[0]
                yield entry(f"{operation}_low_store_{position}_{rounding}", f"""
    cvt.{rounding}.f16.f32 h4, f0;
    {operation}.rn.f16 h15, {', '.join(args)};""", "st.global.b16 [dest], h15;")
        for arithmetic_rounding in ("rn", "rz", "rm", "rp"):
            for operation, arity in (("add", 2), ("mul", 2), ("fma", 3)):
                yield entry(f"float_{operation}_{arithmetic_rounding}_to_half_{rounding}_low", f"""
    cvt.f32.f16 f1, h0;
    cvt.f32.f16 f2, h2;
    {operation}.{arithmetic_rounding}.f32 f3, {', '.join(['f0','f1','f2'][:arity])};
    cvt.{rounding}.f16.f32 h15, f3;""", "st.global.b16 [dest], h15;")
    for comparison in ("eq", "ne", "lt", "le", "gt", "ge", "equ", "neu", "ltu", "leu", "gtu", "geu", "num", "nan"):
        for operation in ("set", "setp"):
            for both_float in (False, True):
                setup = "cvt.rz.f16.f32 h4, f0;\nmov.b32 r4, {h4, h4};"
                rhs = "r1"
                if both_float:
                    setup += "\nmov.b32 f1, r1;\ncvt.rz.f16.f32 h5, f1;\nmov.b32 r5, {h5, h5};"
                    rhs = "r5"
                if operation == "set":
                    body = f"set.{comparison}.f16x2.f16x2 r15, r4, {rhs};"
                else:
                    body = f"setp.{comparison}.f16x2 p|q, r4, {rhs};\nselp.u32 r14, 1, 0, p;\nselp.u32 r15, 2, 0, q;\nor.b32 r15, r15, r14;"
                yield entry(f"{operation}_{comparison}_{'both_float' if both_float else 'float_half'}", setup + "\n" + body)


def implicit_matrix():
    """Isolate explicit arithmetic rounding from input-conversion rounding."""
    for rounding in ("rn", "rz", "rm", "rp"):
        for packed in (False, True):
            fmt = "f16x2" if packed else "f16"
            for operation, arity in (("add", 2), ("mul", 2), ("fma", 3)):
                # FMA requires an explicit rounding modifier in PTX.
                modifiers = ("rn",) if operation == "fma" else ("implicit", "rn")
                for position in range(arity):
                    for modifier in modifiers:
                        args = list(("r4", "r1", "r2") if packed else ("h4", "h0", "h2"))[:arity]
                        args[0], args[position] = args[position], args[0]
                        setup = f"cvt.{rounding}.f16.f32 h4, f0;"
                        if packed:
                            setup += "\nmov.b32 r4, {h4, h4};"
                        suffix = "" if modifier == "implicit" else f".{modifier}"
                        setup += f"\n{operation}{suffix}.{fmt} {'r15' if packed else 'h15'}, {', '.join(args)};"
                        if not packed:
                            setup += "\nmov.b32 r15, {h15, h15};"
                        yield entry(f"{operation}_{'packed' if packed else 'scalar'}_{position}_{rounding}_{modifier}", setup)
        for position in range(3):
            for modifier in ("implicit", "rn"):
                args = ["r4", "r1", "r2"]
                args[0], args[position] = args[position], args[0]
                suffix = "" if modifier == "implicit" else f".{modifier}"
                yield entry(f"mul_add_packed_{position}_{rounding}_{modifier}", f"""
    cvt.{rounding}.f16.f32 h4, f0;
    mov.b32 r4, {{h4, h4}};
    mul{suffix}.f16x2 r5, {args[0]}, {args[1]};
    add{suffix}.f16x2 r15, r5, {args[2]};""")
    # Test whether implicit scalar float addition can fold widened half inputs.
    for modifier in ("implicit", "rn"):
        suffix = "" if modifier == "implicit" else f".{modifier}"
        yield entry(f"half_add_to_float_{modifier}", f"""
    cvt.f32.f16 f0, h0;
    cvt.f32.f16 f1, h1;
    add{suffix}.f32 f3, f0, f1;
    mov.b32 r15, f3;""")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--implicit", action="store_true",
                        help="search implicit versus explicit arithmetic rounding")
    args = parser.parse_args()
    entries = list(implicit_matrix() if args.implicit else matrix())
    args.output.write_text(HEADER + "".join(entries))
    print(f"{len(entries)} runtime-input entries -> {args.output}")


if __name__ == "__main__":
    main()
