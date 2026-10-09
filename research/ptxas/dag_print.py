"""Print ptxas's COP DAG (the IR between PTX and ORI) for each function.

Stops at build_ori_for_function, when the DAG is complete, and walks the nodes
that cop_dag_create_node linked into the function's DAG context.

  DAG_OUT=/tmp/dag.txt gdb -q -batch -x research/ptxas/dag_print.py \
      --args /usr/local/cuda-13.0/bin/ptxas -arch=sm_100 -o k.cubin k.ptx

Node layout: +0 vtable (node class), +8 opcode (COP enum), +0x18 type (ORI type
codes), +0x20 basic block, +0x24 id, +0x48 previous node, +0x99 operand count;
operands are 0x28-byte records from +0xa8: {+0 vtable 0x229e468, +8 type,
+0x18 source node, +0x20 component select (default 0xff03020100)}. Constant
nodes (vtable 0x229e500) keep {kind, value} at +0xa8; symbol nodes (vtable
0x229e558) keep the symbol pointer at +0xa8.
"""
import json
import os
import gdb

CREATE_NODE = 0xA2F6D0     # cop_dag_create_node(dag_ctx, desc, ...)
BUILD_ORI = 0xC173E0       # build_ori_for_function: the DAG is complete here
OPERAND_VT, CONST_VT, SYMBOL_VT = 0x229E468, 0x229E500, 0x229E558
DEFAULT_SWIZZLE = 0xFF03020100
TYPE = {1: '', 6: 'f32', 7: 'f16', 9: 's64', 10: 'u64', 11: 's32', 12: 'u32', 19: 'f64', 20: 'pred',
        26: 'cc', 31: 'bf16'}

here = os.path.dirname(__file__)
dag = json.load(open(os.path.join(here, 'dag_opnames.json')))
ori = {int(k, 16): v['name'] for k, v in json.load(open(os.path.join(here, 'ori_opnames.json'))).items()}


def opname(op):
    if str(op) in dag['names']: return dag['names'][str(op)]
    target = dag['ori'].get(str(op))
    if target and int(target, 16) in ori: return '~' + ori[int(target, 16)]
    return f'op{op}'


inf = gdb.selected_inferior()
u8 = lambda a: inf.read_memory(a, 1).tobytes()[0]
u32 = lambda a: int.from_bytes(inf.read_memory(a, 4).tobytes(), 'little')
u64 = lambda a: int.from_bytes(inf.read_memory(a, 8).tobytes(), 'little')
out = open(os.environ['DAG_OUT'], 'w') if 'DAG_OUT' in os.environ else None
emit = lambda s: print(s, file=out) if out else gdb.write(s + '\n')
contexts = []


def symbol_name(sym):
    """First pointer to a printable C string among the symbol object's first words."""
    for off in range(0, 0x60, 8):
        try:
            p = u64(sym + off)
            s = inf.read_memory(p, 64).tobytes().split(b'\0')[0]
            if len(s) >= 1 and all(32 < c < 127 for c in s): return s.decode()
        except gdb.MemoryError:
            continue
    return f'sym@{sym:x}'


def node_line(n, ids):
    op, ty, block, nid = u32(n + 8), u32(n + 0x18), u32(n + 0x20), u32(n + 0x24)
    vt, args = u64(n), []
    if vt == CONST_VT:
        args.append(f'#{u32(n + 0xac)}' + ('' if u32(n + 0xa8) == 1 else f' (kind {u32(n + 0xa8)})'))
    elif vt == SYMBOL_VT:
        args.append(symbol_name(u64(n + 0xa8)))
    else:
        for i in range(u8(n + 0x99)):
            rec = n + 0xA8 + 0x28 * i
            if u64(rec) != OPERAND_VT: break
            src, swz = u64(rec + 0x18), u64(rec + 0x20)
            text = f'@{ids[src]}' if src in ids else f'?{src:x}'
            args.append(text + ('' if swz == DEFAULT_SWIZZLE else f'.sw{swz:x}'))
    t = TYPE.get(ty, f't{ty}')
    return f'  BB{block:<3} @{nid:<4} {opname(op)}{"." + t if t else ""} {", ".join(args)}'


class CreateNode(gdb.Breakpoint):
    def stop(self):
        ctx = int(gdb.parse_and_eval('$rdi'))
        if not contexts or contexts[-1] != ctx: contexts.append(ctx)
        return False


class BuildOri(gdb.Breakpoint):
    def stop(self):
        if not contexts: return False
        nodes, n = [], u64(contexts[-1] + 0x478)
        while n: nodes.append(n); n = u64(n + 0x48)
        nodes.reverse()
        ids = {a: u32(a + 0x24) for a in nodes}
        emit(f'==== COP DAG ({len(nodes)} nodes)')
        for a in nodes: emit(node_line(a, ids))
        return False


CreateNode(f'*{CREATE_NODE:#x}')
BuildOri(f'*{BUILD_ORI:#x}')
gdb.execute('run')
if out: out.close()
