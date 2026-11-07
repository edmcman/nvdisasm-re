from tests.harness import gpu, synchronization


def operation(arch, source):
    match source:
        case "immediate": return "WARPSYNC 0xffffffff;" if arch == "SM89" else "WARPSYNC.ALL PT;"
        case "register": return "MOV R14, 0xffffffff;\nWARPSYNC R14;"
        case "uniform": return "UMOV UR6, 0xffffffff;\nWARPSYNC UR6;"
        case _: raise ValueError(source)


def body(arch, source, threads):
    return f'''
IMAD R6, R0, 4, RZ;
IADD3 R7, R0, 11, RZ;
@P0 BRA odd_store;
STS [R6], R7;
BRA synchronized;
odd_store:
NOP;
NOP;
STS [R6], R7;
synchronized:
{operation(arch, source)}
LOP3.LUT R9, R0, 1, RZ, 0x96, !PT;
ISETP.LT.U32.AND P3, PT, R9, {threads}, PT;
IMAD R9, R9, 4, RZ;
MOV R8, RZ;
@P3 LDS R8, [R9] @sched(dst_wr_sb=0);
'''


def expected(threads):
    return [(index ^ 1) + 11 if (index ^ 1) < threads else 0 for index in range(threads)]


def fixtures(arch):
    sources = ("immediate", "register", "uniform") if arch == "SM89" else ("immediate", "register")
    return [gpu.Fixture(f"warp_sync_{source}_{threads}", synchronization.image(body(arch, source, threads), arch),
                        "synchronize", bytes(12 * threads), expected(threads), block=threads,
                        scope="launch", exit=True, targets=("WARPSYNC", "STS", "LDS"),
                        modes=("exact", "readable"))
            for source in sources for threads in (7, 32, 37)]
