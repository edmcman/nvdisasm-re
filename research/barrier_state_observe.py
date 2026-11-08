import argparse
import hashlib
import json
from pathlib import Path

from sass.assemble import assemble_cubin
from sass.decode import cubin, pydecode
from tests.harness import gpu


COLS = ("before_bar", "before_warp", "after_bar", "after_warp",
        "joined_bar", "joined_warp", "after_result", "joined_result")


def snapshot(barrier):
    return f"""B2R.BAR R10, {barrier} @sched(dst_wr_sb=0);
B2R.WARP R11 @sched(dst_wr_sb=0);
B2R.RESULT R14 @sched(dst_wr_sb=0);"""


def source(block, barrier, mode):
    other = 14 if barrier == 15 else 15
    match mode:
        case "initial": body = "NOP;"
        case "warp": body = "WARPSYNC 0xffffffff;"
        case "arrive": body = f"BAR.ARV {barrier}, {block};"
        case "count32": body = f"BAR.SYNC {barrier}, 32;"
        case "arrival_results" | "deferred_results":
            operation = "ARV" if mode == "arrival_results" else "SYNC.DEFER_BLOCKING"
            body = f"""ISETP.LT.U32.AND P0, PT, R0, 32, PT;
@!P0 BRA observer;
BAR.{operation} {barrier}, {block};
complete:
B2R.BAR R3, {barrier} @sched(dst_wr_sb=0);
ISETP.NE.U32.AND P1, PT, R3, RZ, PT;
@P1 BRA complete;
BRA measured;
observer:
MOV R14, 2048;
delay:
IADD3 R14, R14, -1, RZ;
ISETP.NE.U32.AND P1, PT, R14, RZ, PT;
@P1 BRA delay;
BAR.{operation} {barrier}, {block};
measured:
"""
        case "sync" | "reuse2" | "reuse3":
            count = {"sync": 1, "reuse2": 2, "reuse3": 3}[mode]
            body = "\n".join([f"BAR.SYNC {barrier}, {block};"] * count)
        case "delayed":
            body = f"""
ISETP.LT.U32.AND P0, PT, R0, 32, PT;
@P0 BRA arrive;
MOV R14, 1024;
delay:
IADD3 R14, R14, -1, RZ;
ISETP.NE.U32.AND P1, PT, R14, RZ, PT;
@P1 BRA delay;
arrive:
BAR.ARV {barrier}, {block};
"""
        case "staggered" | "reverse":
            order = f"IADD3 R3, -R3, {block // 32 - 1}, RZ;" if mode == "reverse" else ""
            body = f"""SHF.R.U32.HI R3, RZ, 5, R0;
{order}
IMAD R14, R3, 2048, RZ;
ISETP.EQ.U32.AND P0, PT, R14, RZ, PT;
@P0 BRA arrive;
delay:
IADD3 R14, R14, -1, RZ;
ISETP.NE.U32.AND P1, PT, R14, RZ, PT;
@P1 BRA delay;
arrive:
BAR.ARV {barrier}, {block};
"""
        case name if name.startswith("pending_"):
            op = name.removeprefix("pending_")
            count = "" if op.startswith("auto_") else f", {block}"
            op = op.removeprefix("auto_")
            op, _, pattern = op.partition("_")
            predicate, setup = "PT", ""
            match pattern:
                case "false": predicate = "!PT"
                case "odd":
                    predicate, setup = "P4", """LOP3.LUT R3, R0, 1, RZ, 0xc0, !PT;
ISETP.NE.U32.AND P4, PT, R3, RZ, PT;"""
                case name if name.startswith("n"):
                    predicate, setup = "P4", f"ISETP.LT.U32.AND P4, PT, R0, {int(name[1:])}, PT;"
            operation = (f"BAR.SYNC {barrier}{count};" if op == "sync"
                         else f"BAR.RED.{op.upper()} {barrier}{count}, {predicate};")
            body = f"""{setup}
ISETP.LT.U32.AND P0, PT, R0, 32, PT;
@!P0 BRA observer;
{operation}
{snapshot(barrier)}
BRA measured;
observer:
MOV R14, 2048;
delay:
IADD3 R14, R14, -1, RZ;
ISETP.NE.U32.AND P1, PT, R14, RZ, PT;
@P1 BRA delay;
{snapshot(barrier)}
{operation}
measured:
"""
        case "exit_odd" | "exit_warp":
            predicate = ("LOP3.LUT R3, R0, 1, RZ, 0xc0, !PT;\nISETP.NE.U32.AND P0, PT, R3, RZ, PT;"
                         if mode == "exit_odd" else "ISETP.LT.U32.AND P0, PT, R0, 32, PT;")
            body = f"{predicate}\n@P0 EXIT;\nBAR.SYNC {barrier};"
        case "exit_pending" | "exit_pending_odd":
            setup = ("ISETP.LT.U32.AND P0, PT, R0, 32, PT;\n@P0 EXIT;" if mode == "exit_pending" else
                     "LOP3.LUT R3, R0, 1, RZ, 0xc0, !PT;\nISETP.NE.U32.AND P0, PT, R3, RZ, PT;\n@P0 EXIT;")
            first = 64 if mode == "exit_pending" else 32
            body = f"""{setup}
ISETP.LT.U32.AND P0, PT, R0, {first}, PT;
@!P0 BRA observer;
BAR.SYNC {barrier};
{snapshot(barrier)}
BRA measured;
observer:
MOV R14, 2048;
delay:
IADD3 R14, R14, -1, RZ;
ISETP.NE.U32.AND P1, PT, R14, RZ, PT;
@P1 BRA delay;
{snapshot(barrier)}
BAR.SYNC {barrier};
measured:
"""
        case name if name.startswith("auto_"):
            operation = name.removeprefix("auto_")
            body = ("BAR.SYNC " + str(barrier) + ";" if operation == "sync" else
                    f"""LOP3.LUT R3, R0, 1, RZ, 0xc0, !PT;
ISETP.NE.U32.AND P0, PT, R3, RZ, PT;
BAR.RED.{operation.upper()} {barrier}, P0;""")
        case _: raise ValueError(f"unknown barrier-state mode {mode}")
    measured = "" if mode.startswith(("pending_", "exit_pending")) else snapshot(barrier)
    join = f"BAR.SYNC {other};" if mode.startswith(("auto_", "exit_")) else f"BAR.SYNC {other}, {block};"
    return f""".target SM89
.kernel barrier_state
.param input, 8, 8
.param output, 8, 8
.registers 32, 8
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=63
MOV R20, c[0x0][$output];
MOV R21, c[0x0][$output+4];
S2R R0, SR_TID.X @sched(dst_wr_sb=0);
S2R R2, SR_CTAID.X @sched(dst_wr_sb=0);
IMAD R2, R2, {block}, R0;
IMAD.WIDE.U32 R20, R2, 32, R20;
B2R.BAR R8, {barrier} @sched(dst_wr_sb=0);
B2R.WARP R9 @sched(dst_wr_sb=0);
{body}
{measured}
{join}
B2R.BAR R12, {barrier} @sched(dst_wr_sb=0);
B2R.WARP R13 @sched(dst_wr_sb=0);
B2R.RESULT R15 @sched(dst_wr_sb=0);
STG.E.128 [R20.64], R8;
STG.E.128 [R20.64+16], R12;
EXIT;
.endkernel
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--extended", action="store_true")
    parser.add_argument("--partial-warps", action="store_true")
    parser.add_argument("--block", type=int, action="append")
    parser.add_argument("--mode", action="append")
    args = parser.parse_args()
    rows = []
    driver = gpu.Driver("SM89")
    try:
        blocks = (17, 32, 33, 45, 64, 128) if args.partial_warps else (64, 96, 128, 256) if args.extended else (32, 64, 128)
        modes = (("auto_sync", "auto_popc", "auto_and", "auto_or") if args.partial_warps else
                 ("count32", "staggered", "reverse", "pending_sync", "pending_popc", "pending_and", "pending_or") if args.extended
                 else ("initial", "warp", "sync", "reuse2", "reuse3", "arrive", "delayed"))
        blocks, modes = args.block or blocks, args.mode or modes
        for block in blocks:
            for barrier in (0, 1, 15):
                for mode in modes:
                    if block == 32 and mode == "delayed": continue
                    text = source(block, barrier, mode)
                    image = assemble_cubin(text)
                    kernel = cubin.kernel_image(image, "barrier_state")
                    targets = [dict(offset=offset, word=kernel["code"][offset:offset + 16].hex())
                               for offset in range(0, len(kernel["code"]), 16)
                               if pydecode.decode("SM89", kernel["code"][offset:offset + 16]).klass.mnemonic == "B2R"]
                    fixture = gpu.Fixture(f"b2r_{block}_{barrier}_{mode}", image, "barrier_state",
                                          bytes(12 * block * 2), None, output_size=32,
                                          block=block, grid=2, scope="launch")
                    outputs = [[[(value >> (32 * word)) & 0xffffffff for word in range(len(COLS))]
                                for value in driver.run(fixture)] for _ in range(2)]
                    rows.append(dict(block=block, barrier=barrier, mode=mode, source=text,
                                     image_sha256=hashlib.sha256(image).hexdigest(),
                                     targets=targets, outputs=outputs))
                    args.out.write_text(json.dumps(dict(
                        arch="SM89", device=driver.device,
                        kind="cooperative barrier-state discovery; no verification credit",
                        columns=COLS, observations=rows), indent=1) + "\n")
                    print(block, barrier, mode, sorted({tuple(value) for run in outputs for value in run}), flush=True)
    finally: driver.close()


if __name__ == "__main__":
    main()
