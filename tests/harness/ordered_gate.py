from sass.semantics import bitvec, ordered


PAIR = ordered.operation("ordered_test_pair", (32,), (32, 1), description="Test multiple typed results.")
FAIL = ordered.operation("ordered_test_fail", description="Test faults after observable effects.")


def warp_sync_body():
    body = ordered.Builder()
    value = body.read_register("R", 1)
    body.emit(ordered.WriteRegister(("R", 1, 1), value + 1))
    ticket, = body.special(ordered.WARP_ARRIVE, bitvec.const(3, 32))
    body.special(ordered.WARP_WAIT, ticket)
    body.special(ordered.WARP_RELEASE, ticket)
    return body.block()


def convergence_body():
    body = ordered.Builder()
    value = body.read_register("R", 1)
    body.emit(ordered.WriteRegister(("R", 1, 1), value + 1))
    body.special(ordered.CONVERGENCE_ENTER, bitvec.const(0, 32), bitvec.const(0x2000, 64), bitvec.const(3, 32))
    ticket, = body.special(ordered.CONVERGENCE_ARRIVE, bitvec.const(0, 32))
    body.special(ordered.CONVERGENCE_WAIT, ticket)
    body.special(ordered.CONVERGENCE_RELEASE, ticket)
    return body.block()


def register_body():
    b = ordered.Builder()
    before = b.read_register("R", 0)
    b.emit(ordered.WriteRegister(("R", 0, 1), before + 1))
    after = b.read_register("R", 0)
    b.emit(ordered.WriteRegister(("R", 1, 1), after))
    return b.block()


def wide_register_body():
    b = ordered.Builder()
    before = b.read_register("R", 0, 2)
    b.emit(ordered.WriteRegister(("R", 0, 2), bitvec.const(0x0000000300000004, 64)))
    after = b.read_register("R", 0, 2)
    b.emit(ordered.WriteRegister(("R", 2, 2), after))
    b.emit(ordered.WriteRegister(("R", 4, 2), before))
    return b.block()


def branch_body(taken):
    b = ordered.Builder()
    condition = b.read_register("P", 0)
    result = b.temp(32)
    yes = ordered.Builder(b.count)
    x = yes.read_memory("ram", bitvec.const(16, 64), 32)
    no = ordered.Builder(yes.count)
    no.special(FAIL)
    b.emit(ordered.If(condition if taken else ~condition, yes.block(x), no.block(bitvec.const(42, 32)), (result,)))
    b.emit(ordered.WriteRegister(("R", 0, 1), result))
    return b.block()


def atomic_body(fail=False):
    b = ordered.Builder()
    inner = ordered.Builder()
    x = inner.read_memory("ram", bitvec.const(16, 64), 32)
    inner.emit(ordered.WriteMemory("ram", bitvec.const(16, 64), x + 1))
    if fail: inner.special(FAIL)
    b.emit(ordered.Atomic(inner.block()))
    return b.block()


def suspension_body():
    b = ordered.Builder()
    before = b.read_register("R", 0)
    b.emit(ordered.WriteRegister(("R", 0, 1), before + 1))
    ticket, = b.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(32, 32), bitvec.const(1, 1))
    result, = b.special(ordered.WAIT, ticket)
    b.emit(ordered.WriteRegister(("R", 1, 1), before + result))
    return b.block()


def ticket_release_body():
    b = ordered.Builder()
    ticket, = b.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(1, 32), bitvec.const(1, 1))
    b.special(ordered.RELEASE_TICKET, ticket)
    b.emit(ordered.WriteRegister(("R", 0, 1), bitvec.const(42, 32)))
    return b.block()


def block_barrier_body(deferred=False, counted=0, exiting=False, lane_location=("R", 0), exit_target=0):
    b = ordered.Builder()
    if exiting:
        lane = b.read_register(*lane_location)
        b.emit(ordered.If(lane != 0, ordered.Block((ordered.Transfer(
            "goto" if exit_target else "return", bitvec.const(exit_target, 64)),))))
    before = b.read_register("R", 1)
    b.emit(ordered.WriteRegister(("R", 1, 1), before + 1))
    arguments = (bitvec.const(0, 32), bitvec.const(1, 1), bitvec.const(3, 32))
    if counted:
        arguments = (arguments[0], bitvec.const(counted, 32), *arguments[1:])
    ticket, = b.special(ordered.BLOCK_ARRIVE_COUNTED if counted else ordered.BLOCK_ARRIVE_IMPLICIT, *arguments)
    b.special(ordered.BLOCK_DEFER if deferred else ordered.BLOCK_WAIT_READY, ticket)
    b.special(ordered.BLOCK_RELEASE, ticket)
    if not deferred:
        result, = b.special(ordered.BLOCK_RESULT)
        b.emit(ordered.WriteRegister(("R", 3, 1), result))
    return b.block()


def block_barrier_deferred_wait_body():
    b = ordered.Builder()
    before = b.read_register("R", 2)
    b.emit(ordered.WriteRegister(("R", 2, 1), before + 1))
    b.special(ordered.BLOCK_WAIT_DEFERRED)
    result, = b.special(ordered.BLOCK_RESULT)
    b.emit(ordered.WriteRegister(("R", 3, 1), result))
    return b.block()


def snapshot_body():
    b = ordered.Builder()
    lane = b.read_register("R", 0)
    x = b.read_register("R", 1)
    snapshot, = b.special(ordered.CAPTURE, lane, bitvec.const(3, 32), x)
    b.emit(ordered.WriteRegister(("R", 1, 1), bitvec.const(99, 32)))
    ticket, = b.special(ordered.ARRIVE, bitvec.const(0, 32), bitvec.const(2, 32), bitvec.const(1, 1))
    b.special(ordered.WAIT_READY, ticket)
    other, = b.special(ordered.READ_LANE, snapshot, lane ^ 1)
    b.emit(ordered.WriteRegister(("R", 2, 1), other))
    return b.block()


def pair_body():
    b = ordered.Builder()
    x, p = b.special(PAIR, bitvec.const(5, 32))
    b.emit(ordered.WriteRegister(("R", 0, 1), x))
    b.emit(ordered.WriteRegister(("P", 0, 1), p))
    return b.block()


def numerical_body(choice=False):
    from sass.semantics.families import floating
    b = ordered.Builder()
    a, c = b.read_register("R", 0), b.read_register("R", 1)
    value = floating.packed_ftz("add", (a, c))
    if choice: value = bitvec.Expr("sel", (("gate_choice",), (((None,), value),)), value.w)
    b.emit(ordered.WriteRegister(("R", 2, 1), value))
    return b.block()


def numerical_outputs_body():
    from sass.semantics.families import floating
    b = ordered.Builder()
    a, c = b.read_register("R", 0), b.read_register("R", 1)
    value = floating.packed_ftz("add", (a, c))
    b.assign_from_inputs([(("R", 2, 1), value), (("R", 3, 1), value ^ 1)])
    return b.block()


def repeated_choice_body():
    b = ordered.Builder()
    a, c = b.read_register("R", 0), b.read_register("R", 1)
    choice = lambda value: bitvec.Expr("sel", (("gate_choice",), (((None,), bitvec.zext(value, 64)),)), 64)
    x = b.move(choice(a))
    b.emit(ordered.WriteRegister(("R", 1, 1), bitvec.const(100, 32)))
    y = b.move(choice(c))
    b.emit(ordered.WriteRegister(("R", 2, 2), x + y))
    return b.block()
