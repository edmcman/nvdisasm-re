from sass import launch
from sass.assemble import assemble_cubin
from tests.harness import gpu


def emulator_cases(c):
    return c


def gpu_fixtures(arch="SM89"):
    def fixture(width, threads, block):
        size = ".64" if width == 64 else ""
        address = "[R20.64]" if arch == "SM89" else "desc[UR4][R20.64]"
        image = assemble_cubin(f""".target {arch}
.kernel contend
.param input, 8, 8
.param output, 8, 8
.registers 32, 8
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=63
MOV R4, c[0x0][$input];
MOV R5, c[0x0][$input+4];
MOV R20, c[0x0][$output];
MOV R21, c[0x0][$output+4];
ULDC.64 UR4, c[0x0][{launch.GLOBAL_DESCRIPTOR[arch]}];
MOV R12, 1;
MOV R13, 0;
ATOMG.E.ADD{size} PT, R8, [R4], R12 @sched(dst_wr_sb=0);
IMAD.WIDE.U32 R20, R8, 4, R20;
IADD3 R8, R8, 1, RZ;
STG.E {address}, R8;
EXIT;
.endkernel
""")
        return gpu.Fixture(f"atomic_contend_{width}_{threads}_{block}", image, "contend",
                           bytes(12 * threads), list(range(1, threads + 1)), block=block, scope="launch",
                           exit=True, targets=("ATOMG",), modes=("exact", "readable"))
    return [fixture(width, threads, block) for width in (32, 64) for threads, block in ((32, 32), (128, 64))]
