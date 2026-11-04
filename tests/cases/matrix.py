from sass import launch
from sass.assemble import assemble_cubin, encoding
from sass.decode import pydecode
from sass.sleigh import modifiers
from tests.harness import gpu
from tests.refs import matrix


def emulator_cases(c):
    return c


def tensor_fixtures(arch, source, size, destination_format="F32"):
    from tests.harness import autocases
    word = bytes.fromhex(encoding.encode(arch, "hmma_x8_", size=size, srcfmt=source, dstfmt=destination_format,
                                        Ra=16, Rb=24, Rc=32, Rd=40))
    form = modifiers.settings(arch, pydecode.decode(arch, word))
    cases = autocases.generate(arch, "hmma_x8_", form, native=False, sample=100)
    groups = next(c.request["context"]["warp"]["operand_groups"]
                  for c in cases if c.hardware["pattern"] == "random_0")
    data = b"".join(b"".join(b"".join(value.to_bytes(4, "little") for value in groups[name][lane])
                            .ljust(width, b"\0") for name, width in (("Ra", 16), ("Rb", 8))) + bytes(8)
                    + b"".join(value.to_bytes(4, "little") for value in groups["Rc"][lane]).ljust(16, b"\0")
                    for lane in range(32))
    expected = [sum(value << (32 * j) for j, value in enumerate(
                    matrix.hmma(groups, lane, arch, source, int(size[3:]), destination=destination_format)))
                for lane in range(32)]
    width = 64 if destination_format == "F16" else 128
    def fixture(destination):
        address = lambda pointer, offset=0: (f"[R{pointer}.64+{offset}]" if arch == "SM89"
                                            else f"desc[UR4][R{pointer}.64+{offset}]")
        image = assemble_cubin(f""".target {arch}
.kernel matrix_alias
.param input, 8, 8
.param output, 8, 8
.registers 48, 8
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=63
MOV R4, c[0x0][$input];
MOV R5, c[0x0][$input+4];
MOV R20, c[0x0][$output];
MOV R21, c[0x0][$output+4];
ULDC.64 UR4, c[0x0][{launch.GLOBAL_DESCRIPTOR[arch]}];
S2R R0, SR_TID.X @sched(dst_wr_sb=0);
IMAD.WIDE.U32 R4, R0, 48, R4;
IMAD.WIDE.U32 R20, R0, {width // 8}, R20;
LDG.E.128 R16, {address(4)} @sched(dst_wr_sb=0);
LDG.E.64 R24, {address(4, 16)} @sched(dst_wr_sb=1);
LDG.E.{width} R32, {address(4, 32)} @sched(dst_wr_sb=2);
HMMA.{size}.{destination_format}{'.' + source if source != 'F16' else ''} R{destination}, R16, R24, R32 @sched(dst_wr_sb=0);
NOP;
NOP;
STG.E.{width} {address(20)}, R{destination};
EXIT;
.endkernel
""")
        suffix = "_f16" if destination_format == "F16" else ""
        return gpu.Fixture(f"hmma_{source.lower()}_{size}{suffix}_alias_{destination}", image, "matrix_alias", data, expected,
                           input_size=16, output_size=width // 8, block=32, scope="launch", exit=True,
                           targets=("HMMA",), modes=("exact", "readable"))
    return [fixture(destination) for destination in (16, 24, 32)]


def gpu_fixtures(arch="SM89"):
    return [fixture for source, size, destination in (
                ("F16", "16816", "F32"), ("F16", "16816", "F16"), ("BF16", "16816", "F32"),
                ("F16", "1688", "F32"), ("F16", "1688", "F16"), ("BF16", "1688", "F32"),
                ("TF32", "1684", "F32"), ("TF32", "1688", "F32"))
            for fixture in tensor_fixtures(arch, source, size, destination)]
