from dataclasses import replace
from sass import launch
from sass.assemble import assemble_cubin
from tests.harness import gpu


def image(arch, width=128, fill=False, enabled=True, encoded_wait=False, deferred=False, uniform=False, board=0, empty=False,
          predicated_wait=False):
    global_address = "[R20.64]" if arch == "SM89" else "desc[UR4][R20.64]"
    output_address = "[R24.64]" if arch == "SM89" else "desc[UR4][R24.64]"
    predicate = "PT" if enabled else "!PT"
    instruction = f"LDGSTS.E.{width}{'.ZFILL' if fill else ''}"
    wait = f"{'@!PT ' if predicated_wait else ''}NOP @sched(req_bit_set={1 << board});" if encoded_wait else f"DEPBAR.LE SB{board}, 0;"
    barrier = "BAR.SYNC.DEFER_BLOCKING 0;" if deferred else ""
    setup = "UMOV UR6, 32; UMOV UR7, 0x10000000; IADD3 R3, R1, -32, RZ;" if uniform else ""
    destination = "R3+UR6" if uniform else "R1"
    if uniform: global_address = "desc[UR7][R20.64]"
    empty_group = f"LDGDEPBAR @sched(dst_wr_sb={board});" if empty else ""
    return assemble_cubin(f""".target {arch}
.kernel async_copy
.param input, 8, 8
.param output, 8, 8
.registers 32, 8
.shared 2048
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=0
MOV R1, c[0x0][0x28];
MOV R20, c[0x0][$input];
MOV R21, c[0x0][$input+4];
MOV R24, c[0x0][$output];
MOV R25, c[0x0][$output+4];
ULDC.64 UR4, c[0x0][{launch.GLOBAL_DESCRIPTOR[arch]}];
S2R R0, SR_TID.X @sched(dst_wr_sb=1);
IMAD R1, R0, 64, RZ @sched(req_bit_set=2);
S2R R2, SR_CTAID.X @sched(dst_wr_sb=1);
IMAD R2, R2, 32, R0 @sched(req_bit_set=2);
IMAD.WIDE.U32 R20, R2, 48, R20;
IMAD.WIDE.U32 R24, R2, 16, R24;
MOV R8, 0x11223344;
MOV R9, 0x55667788;
MOV R10, 0x99aabbcc;
MOV R11, 0xddeeff00;
STS.128 [R1], R8;
STS.128 [R1+16], R8;
BAR.SYNC 0;
MOV R12, RZ;
MOV R13, RZ;
MOV R14, RZ;
MOV R15, RZ;
{setup}
{empty_group}
{instruction} [{destination}], {global_address}, {predicate};
LDGDEPBAR @sched(dst_wr_sb={board});
{instruction} [{destination}+16], {global_address}, {predicate};
LDGDEPBAR @sched(dst_wr_sb={board});
DEPBAR.LE SB{board}, 1;
LDS.{width} R8, [R1] @sched(dst_wr_sb=1);
{wait}
{barrier}
LDS.{width} R12, [R1+16] @sched(dst_wr_sb=2);
MOV R9, R9 @sched(req_bit_set=6);
IADD3 R8, R8, R12, RZ;
IADD3 R9, R9, R13, RZ;
IADD3 R10, R10, R14, RZ;
IADD3 R11, R11, R15, RZ;
STG.E.128 {output_address}, R8;
EXIT;
.endkernel
""")


def fixtures(arch):
    out = []
    for width in (32, 64, 128):
        for fill, enabled in ((False, True), (False, False), (True, False), (True, True)):
            for encoded, deferred in ((False, False), (True, False), (False, True)):
                words = [tuple((i + 1) << (j * 4) for j in range(4)) for i in range(64)]
                data = b"".join(b"".join(word.to_bytes(4, "little") for word in row) + bytes(32) for row in words)
                initial = (0x11223344, 0x55667788, 0x99aabbcc, 0xddeeff00)
                expected = []
                for row in words:
                    a = row if enabled else (0,) * 4
                    expected.append(sum(((2 * a[j]) & 0xffffffff if j < width // 32 else initial[j])
                                        << (32 * j) for j in range(4)))
                name = f"async_{width}_{fill}_{enabled}_{encoded}_{deferred}"
                out.append(gpu.Fixture(name, image(arch, width, fill, enabled, encoded, deferred),
                                       "async_copy", data, expected, input_size=16, output_size=16,
                                       block=32, scope="launch", exit=True,
                                       targets=("LDGSTS", "LDGDEPBAR", "DEPBAR"), modes=("exact", "readable")))
    base = next(f for f in out if f.name == "async_128_False_True_False_False")
    out.append(replace(base, name="async_uniform_descriptor", image=image(arch, uniform=True)))
    out.extend(replace(base, name=name, image=image(arch, **options)) for name, options in (
        ("async_empty_group", dict(empty=True)),
        ("async_scoreboard3", dict(board=3)),
        ("async_scoreboard3_encoded", dict(board=3, encoded_wait=True)),
        ("async_predicated_off_wait", dict(encoded_wait=True, predicated_wait=True)),
        ("async_scoreboard3_predicated_off_wait", dict(board=3, encoded_wait=True, predicated_wait=True))))
    return out
