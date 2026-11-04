from sass import launch
from sass.assemble import assemble_cubin
from sass.decode import cubin
from tests.harness import gpu
from tests.refs import barrier


NESTED = '''
BSSY B0, joined;
@P0 BRA odd;
BSSY B1, nested;
MOV R8, 3;
@P1 BRA inner;
MOV R8, 5;
inner:
BSYNC B1;
nested:
BRA outer;
odd:
MOV R8, 7;
outer:
BSYNC B0;
joined:
VOTE.ANY R9, PT, PT;
BSSY B0, reused;
@P1 MOV R8, 11;
BSYNC B0;
reused:
IADD3 R8, R8, R9, RZ;
'''
EXITED = '''
BSSY B0, joined;
@P0 EXIT;
MOV R8, 3;
BSYNC B0;
joined:
VOTE.ANY R8, PT, PT;
'''
PREDICATED = '''
BSSY P0, B0, joined;
@!P0 BRA skip;
MOV R8, 7;
BSYNC P0, B0;
joined:
VOTE.ANY R8, PT, PT;
BRA done;
skip:
MOV R8, 3;
done:
'''
COUNTED = '''
ISETP.LT.U32.AND P2, PT, R0, 32, PT;
@!P2 BRA upper;
MOV R8, 7;
BAR.SYNC 1, 32;
BRA done;
upper:
MOV R8, 9;
BAR.SYNC 2, 32;
done:
BAR.SYNC 0;
BAR.SYNC 0;
'''
TARGET = '''
BSSY B0, joined;
MOV R8, 3;
BSYNC B0;
MOV R8, 9;
joined:
'''
COMBINED = NESTED + '''
IMAD R6, R0, 4, RZ;
STS [R6], R8 @sched(src_rel_sb=0);
BAR.SYNC.DEFER_BLOCKING 0;
MOV R12, 0x40000000;
IADD3 R11, R0, 1, RZ;
LOP3.LUT R11, R11, 63, RZ, 0xc0, !PT;
IMAD R11, R11, 4, RZ;
MUFU.RCP R10, R12 @sched(dst_wr_sb=0);
LDS R8, [R11] @sched(dst_wr_sb=0);
IADD3 R8, R8, R10, RZ;
'''
BAR_EXITED = '''
@P0 EXIT;
MOV R8, 7;
BAR.SYNC 0;
'''
COUNTED_REGISTERS = 'MOV R14, 1;\nMOV R15, 32;\n' + COUNTED.replace(
    'BAR.SYNC 1, 32;', 'BAR.SYNC R14, 32;').replace('BAR.SYNC 2, 32;', 'BAR.SYNC 2, R15;')
COUNTED_DEFERRED = COUNTED_REGISTERS.replace('BAR.SYNC ', 'BAR.SYNC.DEFER_BLOCKING ')


def image(body, arch='SM89', name='synchronize'):
    address = '[R20.64]' if arch == 'SM89' else 'desc[UR4][R20.64]'
    return assemble_cubin(f'''.target {arch}
.kernel {name}
.param input, 8, 8
.param output, 8, 8
.registers 32, 8
.shared 256
.schedule usched_info=WAIT15_END_GROUP, req_bit_set=63
MOV R1, c[0x0][0x28];
MOV R20, c[0x0][$output];
MOV R21, c[0x0][$output+4];
ULDC.64 UR4, c[0x0][{launch.GLOBAL_DESCRIPTOR[arch]}];
S2R R0, SR_TID.X @sched(dst_wr_sb=0);
IMAD.WIDE.U32 R20, R0, 4, R20;
STG.E {address}, RZ;
LOP3.LUT R3, R0, 1, RZ, 0xc0, !PT;
ISETP.NE.U32.AND P0, PT, R3, RZ, PT;
LOP3.LUT R3, R0, 2, RZ, 0xc0, !PT;
ISETP.NE.U32.AND P1, PT, R3, RZ, PT;
{body}
STG.E {address}, R8;
EXIT;
.endkernel
''')


def request(body, threads=32, block=32, order='round_robin', arch='SM89'):
    kernel = cubin.kernel_image(image(body, arch), 'synchronize')
    return launch.kernel_request(kernel, (gpu.INPUT, gpu.OUTPUT), [], threads, block,
                                 dict(address=gpu.OUTPUT), scope='launch', schedule=order)


def nested_expected(threads):
    return [((1 << min(32, threads - i // 32 * 32)) - 1)
            + (11 if i & 2 else 7 if i & 1 else 5) & 0xffffffff for i in range(threads)]


def fixtures(arch):
    cases = (
        ('nested', NESTED, nested_expected(32), ('BSSY', 'BSYNC', 'VOTE')),
        ('exited', EXITED, [0 if i & 1 else 0x55555555 for i in range(32)], ('BSSY', 'BSYNC', 'VOTE')),
        ('predicated', PREDICATED, [0xaaaaaaaa if i & 1 else 3 for i in range(32)], ('BSSY', 'BSYNC')),
        ('counted', COUNTED, [7] * 32 + [9] * 32, ('BAR',)),
        ('counted_registers', COUNTED_REGISTERS, [7] * 32 + [9] * 32, ('BAR',)),
        ('counted_deferred', COUNTED_DEFERRED, [7] * 32 + [9] * 32, ('BAR.SYNC.DEFER_BLOCKING',)),
        ('target', TARGET, [9] * 32, ('BSSY', 'BSYNC')),
        ('bar_exited', BAR_EXITED, [0 if i & 1 else 7 for i in range(32)], ('BAR',)),
        ('combined', COMBINED, [nested_expected(64)[(i + 1) % 64] + 0x3f000000 & 0xffffffff
                                for i in range(64)], ('BSSY', 'BSYNC', 'BAR.SYNC.DEFER_BLOCKING', 'LDS', 'STS', 'MUFU.RCP')),
    )
    return [gpu.Fixture('sync_' + name, image(body, arch), 'synchronize', bytes(12 * len(expected)),
                        expected, block=len(expected), scope='launch', exit=True,
                        targets=targets, modes=('exact', 'readable'))
            for name, body, expected, targets in cases]


def reductions(arch):
    selected = []
    for op in ("POPC", "AND", "OR"):
        for pred in ("P0", "!P0", "PT", "!PT"):
            values = [bool(i & 1) if pred == "P0" else not bool(i & 1) if pred == "!P0" else pred == "PT"
                      for i in range(64)]
            result = barrier.reduce(op, values)
            body = f"""BAR.RED.{op} 0, {pred};
B2R.RESULT R8, P2 @sched(dst_wr_sb=0);
MOV R9, RZ;
@P2 MOV R9, 0x10000;
IADD3 R8, R8, R9, RZ;
"""
            selected.append(gpu.Fixture(f"bar_result_{op}_{pred}", image(body, arch), "synchronize",
                                        bytes(12 * 64), [(result + (0x10000 if result else 0)) & 0xffffffff] * 64,
                                        block=64, scope="launch", exit=True, targets=("BAR.RED", "B2R.RESULT"),
                                        modes=("exact", "readable")))
    body = """BAR.RED.POPC 0, P0;
B2R.RESULT R8 @sched(dst_wr_sb=0);
BAR.RED.OR 0, !PT;
B2R.RESULT R9, P2 @sched(dst_wr_sb=0);
IADD3 R8, R8, R9, RZ;
@P2 MOV R8, 7;
"""
    selected.append(gpu.Fixture("bar_result_reuse", image(body, arch), "synchronize", bytes(12 * 64), [32] * 64,
                                block=64, scope="launch", exit=True, targets=("BAR.RED", "B2R.RESULT"),
                                modes=("exact", "readable")))
    return selected


def predicate_values(predicate, count):
    match predicate:
        case "P0": return [bool(i & 1) for i in range(count)]
        case "!P0": return [not bool(i & 1) for i in range(count)]
        case "PT" | None: return [True] * count
        case "!PT": return [False] * count
        case _: raise ValueError(predicate)


def barrier_states(arch):
    selected = []
    for threads in (17, 32, 33, 45, 64):
        for mode in ("RESULT", "WARP"):
            for operation, op, values in (
                ("BAR.SYNC 0;", "SYNC", [True] * threads),
                ("BAR.SYNC 0, 32;", "SYNC", [True] * 32),
                ("BAR.RED.POPC 0, P0;", "POPC", [bool(i & 1) for i in range(threads)]),
                ("BAR.RED.AND 0, PT;", "AND", [True] * threads),
                ("BAR.RED.OR 0, !PT;", "OR", [False] * threads),
            ):
                if ", 32;" in operation and threads % 32: continue
                result = len(values) if op == "SYNC" else barrier.reduce(op, values)
                result = barrier.warp(result) if mode == "WARP" else result
                body = f"{operation}\nB2R.{mode} R8 @sched(dst_wr_sb=0);"
                count = "explicit" if ", 32;" in operation else "implicit"
                selected.append(gpu.Fixture(f"bar_state_{threads}_{mode}_{op}_{len(values)}_{count}", image(body, arch),
                                           "synchronize", bytes(12 * threads), [result] * threads,
                                           block=threads, scope="launch", exit=True, targets=("BAR", f"B2R.{mode}"),
                                           modes=("exact", "readable")))
    for op in ("SYNC", "ARV", "POPC", "AND", "OR"):
        predicate = {"POPC": "P0", "AND": "PT", "OR": "!PT"}.get(op)
        for index in (0, 1, 15):
            wait = f"""complete:
B2R.BAR R9, {index} @sched(dst_wr_sb=0);
ISETP.NE.U32.AND P3, PT, R9, RZ, PT;
@P3 BRA complete;""" if op == "ARV" else ""
            combinations = [(64, mode, predicate) for mode in ("BAR", "RESULT", "WARP")] + ([(0, "BAR", predicate)] if op != "ARV" else [])
            if op in ("AND", "OR") and index == 0: combinations += [(64, "BAR", "P0"), (0, "BAR", "P0")]
            for count, mode, pred in combinations:
                encoded = f", {count}" if count else ""
                operation = f"BAR.RED.{op} {index}{encoded}, {pred};" if pred else f"BAR.{op} {index}{encoded};"
                result = barrier.reduce(op, predicate_values(pred, 64)) if pred else 64
                update = (f"B2R.{mode} R9 @sched(dst_wr_sb=0);\nIADD3 R8, R8, R9, RZ;" if mode != "BAR" else "")
                body = f"""MOV R8, RZ;
ISETP.LT.U32.AND P2, PT, R0, 32, PT;
@!P2 BRA observer;
{operation}
{wait}
BRA measured;
observer:
pending:
B2R.BAR R8, {index} @sched(dst_wr_sb=0);
ISETP.EQ.U32.AND P3, PT, R8, RZ, PT;
@P3 BRA pending;
{operation}
measured:
{update}
B2R.BAR R9, {index} @sched(dst_wr_sb=0);
ISETP.NE.U32.AND P3, PT, R9, RZ, PT;
@P3 MOV R8, 0xdeadbeef;
"""
                value = 0 if mode == "BAR" else barrier.warp(result) if mode == "WARP" else result
                pending = barrier.pending(op, 2 if count else 0, 1, predicate_values(pred, 32))
                expected = [0 if op == "ARV" else value] * 32 + [(pending + value) & 0xffffffff] * 32
                selected.append(gpu.Fixture(f"bar_pending_{op}_{index}_{mode}_{count}_{pred}", image(body, arch), "synchronize",
                                           bytes(12 * 64), expected, block=64, scope="launch", exit=True,
                                           targets=("BAR", "B2R") + ((f"B2R.{mode}",) if mode != "BAR" else ()),
                                           modes=("exact", "readable")))
    for body, expected, name in (
        ("B2R.RESULT R8 @sched(dst_wr_sb=0);", 0, "initial_result"),
        ("B2R.WARP R8 @sched(dst_wr_sb=0);", 0, "initial_warp"),
        (("WARPSYNC 0xffffffff;" if arch in ("SM75", "SM80", "SM86", "SM89") else "WARPSYNC.ALL PT;")
         + "\nB2R.WARP R8 @sched(dst_wr_sb=0);", 0, "warpsync"),
        ("BAR.SYNC 0;\nB2R.RESULT R8 @sched(dst_wr_sb=0);\nB2R.RESULT R9 @sched(dst_wr_sb=0);\nIADD3 R8, R8, R9, RZ;", 128, "repeat_result"),
        ("BAR.RED.OR 0, PT;\nBAR.SYNC 1;\nB2R.RESULT R8 @sched(dst_wr_sb=0);", 64, "overwrite_result"),
        ("BAR.SYNC.DEFER_BLOCKING 0;\nB2R.RESULT R8 @sched(dst_wr_sb=0);", 64, "deferred_result"),
        ("BAR.SYNC.DEFER_BLOCKING 0;\nB2R.WARP R8 @sched(dst_wr_sb=0);", 64, "deferred_warp"),
    ):
        selected.append(gpu.Fixture(f"bar_state_{name}", image(body, arch), "synchronize", bytes(12 * 64),
                                   [expected] * 64, block=64, scope="launch", exit=True, targets=("B2R",),
                                   modes=("exact", "readable")))
    for threads in (96, 128):
        for exited in ("odd", "warp"):
            predicate = "P0" if exited == "odd" else "P2"
            body = f"""ISETP.LT.U32.AND P2, PT, R0, 32, PT;
@{predicate} EXIT;
BAR.SYNC 0;
B2R.RESULT R8 @sched(dst_wr_sb=0);
"""
            values = [0 if (bool(i & 1) if exited == "odd" else i < 32)
                      else threads // 2 if exited == "odd" else threads - 32 for i in range(threads)]
            selected.append(gpu.Fixture(f"bar_exited_result_{threads}_{exited}", image(body, arch),
                                       "synchronize", bytes(12 * threads), values, block=threads,
                                       scope="launch", exit=True, targets=("BAR", "B2R.RESULT"),
                                       modes=("exact", "readable")))
    for exited in ("warp", "odd"):
        threads, first, total = (96, 64, 64) if exited == "warp" else (64, 32, 32)
        predicate = "P2" if exited == "warp" else "P0"
        body = f"""MOV R8, RZ;
ISETP.LT.U32.AND P2, PT, R0, 32, PT;
@{predicate} EXIT;
ISETP.LT.U32.AND P2, PT, R0, {first}, PT;
@!P2 BRA observer;
BAR.SYNC 0;
BRA measured;
observer:
pending:
B2R R8, 0 @sched(dst_wr_sb=0);
ISETP.EQ.U32.AND P3, PT, R8, RZ, PT;
@P3 BRA pending;
BAR.SYNC 0;
measured:
B2R.RESULT R9 @sched(dst_wr_sb=0);
IADD3 R8, R8, R9, RZ;
"""
        pending = barrier.pending("SYNC", 0, 1, [True] * (32 if exited == "warp" else 16))
        expected = [0 if (i < 32 if exited == "warp" else bool(i & 1))
                    else total if i < first else pending + total for i in range(threads)]
        selected.append(gpu.Fixture(f"bar_exited_pending_{exited}", image(body, arch), "synchronize",
                                   bytes(12 * threads), expected, block=threads, scope="launch",
                                   exit=True, targets=("BAR", "B2R", "B2R.RESULT"),
                                   modes=("exact", "readable")))
    return selected


def indirect_branches(arch):
    positive = """MOV R4, 48;
MOV R5, RZ;
MOV R8, 7;
BRX {predicate}, R4 -0x10;
MOV R8, 3;
BRA done;
NOP;
done:
"""
    selected = [gpu.Fixture(f"brx_positive_{pred}", image(positive.format(predicate=pred), arch),
                             "synchronize", bytes(12 * 32),
                             [7 if bool(i & 1) == (pred == "P0") else 3 for i in range(32)],
                             block=32, scope="block", exit=True, targets=("BRX",), modes=("exact", "readable"))
                for pred in ("P0", "!P0")]
    negative = """MOV R8, RZ;
again:
IADD3 R8, R8, 1, RZ;
ISETP.LT.U32.AND P2, PT, R8, 3, PT;
MOV R4, 0xffffffc0;
MOV R5, 0xffffffff;
BRX P2, R4 -0x10;
"""
    selected.append(gpu.Fixture("brx_negative", image(negative, arch), "synchronize", bytes(12 * 32), [3] * 32,
                                block=32, scope="block", exit=True, targets=("BRX",), modes=("exact", "readable")))
    return selected
