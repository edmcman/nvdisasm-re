from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from sass.semantics import bitvec


@dataclass(frozen=True)
class Opaque:
    name: str


@dataclass(frozen=True)
class Handle:
    index: int
    type: Opaque


def value_type(value):
    return value.type if isinstance(value, Handle) else value.w


@dataclass(frozen=True)
class Operation:
    name: str
    arguments: tuple
    results: tuple
    reads: tuple
    writes: tuple
    suspends: bool
    description: str
    runtime: str | None = None
    native: str | None = None


OPERATIONS = {}


def constant_choice(expression):
    return isinstance(expression, bitvec.Expr) and (bitvec.is_const(expression) or
           expression.op == "sel" and all(bitvec.is_const(arm) for _, arm in expression.args[1]))


def definition_signature(expression):
    arguments = tuple((True, argument.w) if isinstance(argument, bitvec.Expr) and
                      not constant_choice(argument) else (False, argument)
                      for argument in expression.args)
    return expression.op, expression.w, arguments


def definition_accesses_state(expression):
    return definition_properties(*definition_signature(expression))[0]


@lru_cache(maxsize=2048)
def definition_properties(name, width, arguments):
    parameters = tuple(bitvec.param(f"__ordered_argument_{index}", value) if numeric else value
                       for index, (numeric, value) in enumerate(arguments))
    expanded = bitvec.lowered(bitvec.Expr(name, parameters, width))
    leaves = frozenset(bitvec.leaves(expanded, "param"))
    inputs = frozenset(index for index, parameter in enumerate(parameters)
                       if arguments[index][0] and parameter in leaves)
    return any(bitvec.leaves(expanded, kind) for kind in ("reg", "load", "primitive")), inputs


@lru_cache(maxsize=4096)
def accesses_state(expression):
    if not isinstance(expression, bitvec.Expr): return False
    if expression.op in bitvec.DEFINED:
        state, inputs = definition_properties(*definition_signature(expression))
        return state or any(accesses_state(expression.args[index]) for index in inputs)
    return (
        expression.op in ("reg", "load", "primitive") or
        expression.op == "sel" and any(accesses_state(arm) for _, arm in expression.args[1]) or
        any(accesses_state(argument) for argument in expression.args if isinstance(argument, bitvec.Expr)))


@lru_cache(maxsize=4096)
def register_inputs(expression):
    return capture_inputs(expression, False)


@lru_cache(maxsize=4096)
def capture_inputs(expression, memory=True):
    """Common captures of decode alternatives; runtime conditions remain inside their captures."""
    if not isinstance(expression, bitvec.Expr): return ()
    match expression.op:
        case "reg":
            return (expression,) if expression.args[0] in ("R", "UR", "P", "UP") else None
        case "load": return (expression,) if memory else None
        case "primitive": return None
        case "sel":
            inputs = [capture_inputs(arm, memory) for _, arm in expression.args[1]]
            return inputs[0] if inputs and inputs[0] is not None and all(
                inputs_ is not None and tuple(map(id, inputs_)) == tuple(map(id, inputs[0])) for inputs_ in inputs) else None
        case "ite":
            condition, yes, no = (capture_inputs(argument, memory) for argument in expression.args)
            if condition is None or yes is None or no is None: return None
            captured = set(condition)
            yes, no = (tuple(value for value in branch if value not in captured) for branch in (yes, no))
            if tuple(map(id, yes)) == tuple(map(id, no)): return (*condition, *yes)
            return (expression,) if memory else None
        case op if op in bitvec.DEFINED:
            state, inputs = definition_properties(*definition_signature(expression))
            if state: return None
            arguments = [expression.args[index] for index in sorted(inputs)]
        case _: arguments = expression.args
    inputs = [capture_inputs(argument, memory) for argument in arguments if isinstance(argument, bitvec.Expr)]
    return None if any(value is None for value in inputs) else tuple(dict.fromkeys(
        source for value in inputs for source in value))


def operation(name, arguments=(), results=(), *, reads=(), writes=(), suspends=False, description, runtime=None, native=None):
    if not name or not description: raise ValueError("operation needs a name and documentation")
    if isinstance(reads, str) or isinstance(writes, str): raise TypeError("operation state accesses must be sequences of names")
    arguments, results = tuple(arguments), tuple(results)
    for type_ in (*arguments, *results):
        if not isinstance(type_, Opaque) and (type(type_) is not int or type_ <= 0):
            raise TypeError("invalid operation value type")
    if native and not suspends and (len(results) > 1 or results and isinstance(results[0], Opaque)):
        raise ValueError("ordinary typed native binding allows at most one numeric result")
    spec = Operation(name, tuple(arguments), tuple(results), tuple(reads), tuple(writes), suspends, description, runtime, native)
    if name in OPERATIONS: raise ValueError(f"operation already defined: {name}")
    OPERATIONS[name] = spec
    return spec


SNAPSHOT = Opaque("warp_snapshot")
SNAPSHOT64 = Opaque("warp_snapshot64")
WORD_SNAPSHOT = Opaque("warp_word_group")
TICKET = Opaque("barrier_ticket")
CAPTURE = operation("warp_capture", (32, 32, 32), (SNAPSHOT,), reads=("warp",), writes=("snapshot",), suspends=True,
                    native="warp_capture",
                    description="Capture participating lanes' values before destination writes.")
CAPTURE64 = operation("warp_capture64", (32, 32, 64), (SNAPSHOT64,), reads=("warp",), writes=("snapshot",), suspends=True,
                      native="warp_capture64",
                      description="Capture participating lanes' 64-bit values before destination writes.")
CAPTURE_PREDICATE = operation("warp_capture_predicate", (32, 32, 1), (SNAPSHOT,), reads=("warp",),
                              writes=("snapshot",), suspends=True, native="warp_capture_predicate",
                              description="Capture participating lanes' predicates before destination writes.")
CAPTURE_WORDS = {width: operation(f"warp_capture_words{width // 32}", (32, 32, 32, width), (WORD_SNAPSHOT,),
                                 reads=("warp",), writes=("snapshot",), suspends=True,
                                 description=f"Capture each participating lane's {width // 32} operand words from the explicitly selected group.")
                 for width in (32, 64, 128)}
READ_WORD = operation("snapshot_word", (WORD_SNAPSHOT, 32, 32), (32,), reads=("snapshot",),
                      native="snapshot_word", description="Read one indexed word of a lane's retained operand group.")
RELEASE_WORDS = operation("snapshot_release_words", (WORD_SNAPSHOT,), writes=("snapshot",),
                         native="snapshot_release_words",
                         description="Release this participant's retained operand group.")
READ_LANE = operation("snapshot_lane", (SNAPSHOT, 32), (32,), reads=("snapshot",),
                      description="Read a lane from a retained warp snapshot.")
READ_LANE64 = operation("snapshot_lane64", (SNAPSHOT64, 32), (64,), reads=("snapshot",),
                        description="Read a 64-bit lane from a retained warp snapshot.")
RELEASE_SNAPSHOT = operation("snapshot_release", (SNAPSHOT,), writes=("snapshot",),
                             native="snapshot_release",
                             description="Release this participant's retained warp snapshot.")
RELEASE_SNAPSHOT64 = operation("snapshot_release64", (SNAPSHOT64,), writes=("snapshot",),
                               native="snapshot_release64",
                               description="Release this participant's retained 64-bit warp snapshot.")
BALLOT = operation("snapshot_ballot", (SNAPSHOT,), (32,), reads=("snapshot",),
                   native="snapshot_reduce<0>",
                   description="Return the mask of captured predicates that are true.")
MATCH = operation("snapshot_match", (SNAPSHOT, 32), (32,), reads=("snapshot",),
                  native="snapshot_reduce<1>",
                  description="Return the captured lanes whose 32-bit values equal the selected lane's.")
MATCH64 = operation("snapshot_match64", (SNAPSHOT64, 32), (32,), reads=("snapshot",),
                    native="snapshot_reduce<2>",
                    description="Return the captured lanes whose 64-bit values equal the selected lane's.")
REDUCTIONS = {name: operation(f"snapshot_redux_{name}", (SNAPSHOT,), (32,), reads=("snapshot",),
                             native=f"snapshot_reduce<{index + 3}>",
                             description=description) for index, (name, description) in enumerate((
    ("and", "Return the bitwise AND of captured 32-bit values."),
    ("or", "Return the bitwise OR of captured 32-bit values."),
    ("xor", "Return the bitwise XOR of captured 32-bit values."),
    ("add", "Return the wrapping sum of captured 32-bit values."),
    ("umin", "Return the unsigned minimum of captured 32-bit values."),
    ("umax", "Return the unsigned maximum of captured 32-bit values."),
    ("smin", "Return the signed minimum of captured 32-bit values."),
    ("smax", "Return the signed maximum of captured 32-bit values."),
))}
ARRIVE = operation("barrier_arrive", (32, 32, 1), (TICKET,), reads=("barrier",), writes=("barrier", "ticket"),
                   description="Arrive once at a barrier generation and retain its ticket.")
WAIT = operation("barrier_wait", (TICKET,), (32,), reads=("barrier",), suspends=True,
                 description="Wait for this ticket's generation and return its reduction.")
WAIT_READY = operation("barrier_wait_ready", (TICKET,), reads=("barrier",), suspends=True,
                       description="Wait for this ticket's generation without a reduction result.")
RELEASE_TICKET = operation("barrier_release_ticket", (TICKET,), writes=("ticket",),
                           native="barrier_release_ticket",
                           description="Release this participant's ticket without cancelling its arrival or an existing wait.")
BLOCK_TICKET = Opaque("block_barrier_ticket")
BLOCK_ARRIVE_IMPLICIT = operation("block_barrier_arrive_implicit", (32, 1, 32), (BLOCK_TICKET,),
                                 reads=("barrier", "block", "environment"), writes=("barrier", "ticket", "events"),
                                 description="Arrive at the barrier ID with an explicit predicate and reduction kind (0: synchronization, 1: AND, 2: OR, 3: POPC). Cooperative participants are the block's threads, excluding exited non-arrivers. Standalone execution requires explicit synchronization acknowledgement.")
BLOCK_ARRIVE_COUNTED = operation("block_barrier_arrive_counted", (32, 32, 1, 32), (BLOCK_TICKET,),
                                reads=("barrier", "block", "environment"), writes=("barrier", "ticket", "events"),
                                description="Arrive at the barrier ID with an explicit participant count, predicate and reduction kind (0: synchronization, 1: AND, 2: OR, 3: POPC). Cooperative counts must be positive multiples of 32 within the block size. Standalone execution requires explicit synchronization acknowledgement.")
BLOCK_WAIT_READY = operation("block_barrier_wait_ready", (BLOCK_TICKET,), reads=("barrier",),
                            writes=("barrier",), suspends=True,
                            description="Wait for this participant's retained block barrier generation.")
BLOCK_DEFER = operation("block_barrier_defer", (BLOCK_TICKET,), writes=("barrier",),
                       description="Retain this participant's generation as its deferred barrier.")
BLOCK_RELEASE = operation("block_barrier_release_ticket", (BLOCK_TICKET,), writes=("ticket",),
                         native="block_barrier_release_ticket",
                         description="Release a block barrier handle without cancelling its arrival, deferred barrier, or issued wait.")
BLOCK_WAIT_DEFERRED = operation("block_barrier_wait_deferred", reads=("barrier",), writes=("barrier",),
                               suspends=True, native="block_barrier_wait_deferred",
                               description="Wait for this participant's pending deferred barrier, if present. An isolated environment without a coordinator has no pending generation.")
BLOCK_RESULT = operation("block_barrier_result", results=(32,), reads=("barrier", "environment"),
                        native="block_barrier_result",
                        description="Read this participant's published block barrier reduction result, or the standalone environment's explicitly supplied result.")
BLOCK_STATE = operation("block_barrier_state", (32,), (32,), reads=("barrier", "environment"),
                       native="block_barrier_state", description="Read the explicit barrier ID's current block barrier state, or the standalone environment's explicitly supplied state.")
WARP_TICKET = Opaque("warp_sync_ticket")
WARP_ARRIVE = operation("warp_sync_arrive", (32,), (WARP_TICKET,),
                        reads=("warp", "environment"), writes=("warp_barrier", "ticket", "events"),
                        description="Arrive once with an explicit member mask and retain a warp synchronization ticket. The mask must include this lane; participating live lanes must arrive with matching masks. Standalone execution requires explicit synchronization acknowledgement.")
WARP_ARRIVE_EXCLUSIVE = operation("warp_sync_arrive_exclusive", (32,), (WARP_TICKET,),
                                  reads=("environment",), writes=("ticket", "events"),
                                  description="Request explicit standalone acknowledgement of exclusive warp synchronization with this mask. Cooperative execution is unsupported; an untaken predicate makes no request.")
WARP_WAIT = operation("warp_sync_wait", (WARP_TICKET,), reads=("warp_barrier",), suspends=True,
                      description="Wait for the ticket's live masked participants to arrive, retaining the ticket's generation across suspension.")
WARP_RELEASE = operation("warp_sync_release_ticket", (WARP_TICKET,), writes=("ticket",),
                         native="warp_sync_release_ticket",
                         description="Release the participant's ticket without cancelling its arrival or an existing wait.")
BARRIER_ACKS = {
    name: operation(f"barrier_{name}_ack", arguments, reads=("environment",), writes=("events",),
                    description=description + " Cooperative interpretation remains unresolved.")
    for name, arguments, description in (
        ("packed_arrive", (32, 32), "Acknowledge two captured packed barrier operands in the standalone environment."),
        ("packed_wait", (32, 32), "Acknowledge waiting with two captured packed barrier operands in the standalone environment."),
        ("packed_defer", (32, 32), "Acknowledge deferred waiting with two captured packed barrier operands in the standalone environment."),
        ("packed_reduce", (32, 32, 32, 1), "Acknowledge the encoded reduction, two captured packed barrier operands and captured predicate contribution in the standalone environment."),
        ("packed_reduce_defer", (32, 32, 32, 1), "Acknowledge deferred reduction with the encoded reduction, two captured packed barrier operands and captured predicate contribution in the standalone environment."),
        ("scan", (32, 32), "Acknowledge scanning with the captured barrier ID and count in the standalone environment."),
        ("all", (), "Acknowledge synchronization of all barriers in the standalone environment."),
        ("all_defer", (), "Acknowledge deferred synchronization of all barriers in the standalone environment."))}
WARP_ACKS = {
    name: operation(f"warp_{name}_ack", arguments, reads=("environment",), writes=("events",),
                    description=description + " Cooperative execution remains unsupported.")
    for name, arguments, description in (
        ("mask", (32,), "Acknowledge synchronization with the captured constant-bank mask in the standalone environment."),
        ("collective_all", (64,), "Acknowledge collective synchronization with the explicit target and implicitly encoded ALL mode in the standalone environment."),
        ("collective_mask", (32, 64), "Acknowledge collective synchronization with the captured mask and explicit target in the standalone environment."),
        ("relative_all", (64, 32), "Acknowledge collective synchronization with the encoded immediate, REL control and implicitly encoded ALL mode in the standalone environment."),
        ("relative_mask", (32, 64, 32), "Acknowledge collective synchronization with the captured mask, encoded immediate and REL control in the standalone environment."))}
COORDINATED = operation("cooperative_context", results=(1,), reads=("environment",),
                        native="cooperative_context",
                        description="Read whether this execution has a shared cooperative coordinator.")
CONVERGENCE_ENTER = operation("convergence_enter", (32, 64, 32), reads=("warp",),
                             writes=("convergence",), native="convergence_enter",
                             description="Enter the explicit convergence ID and target with the captured enabled-member mask. Each member enters once; reusing an active ID faults.")
CONVERGENCE_TICKET = Opaque("convergence_ticket")
CONVERGENCE_ARRIVE = operation("convergence_arrive", (32,), (CONVERGENCE_TICKET,),
                              reads=("convergence",), writes=("convergence", "ticket"),
                              description="Arrive once at this lane's previously entered convergence ID and retain its generation. Missing membership faults.")
CONVERGENCE_WAIT = operation("convergence_wait", (CONVERGENCE_TICKET,), reads=("convergence",),
                            suspends=True, native="convergence_wait",
                            description="Wait for every live member of the retained convergence generation to arrive. Exited members do not block completion.")
CONVERGENCE_RELEASE = operation("convergence_release_ticket", (CONVERGENCE_TICKET,), writes=("ticket",),
                               native="convergence_release_ticket",
                               description="Release this convergence handle without cancelling an issued wait or changing a later generation.")
CONVERGENCE_ACK_ENTER = operation("convergence_ack_enter", (32, 64), reads=("environment",),
                                 writes=("events",),
                                 description="Request explicit standalone BSSY acknowledgement for the convergence ID and target; no concurrent scheduler is introduced.")
CONVERGENCE_ACK_WAIT = operation("convergence_ack_wait", (32,), reads=("environment",), writes=("events",),
                                description="Request explicit standalone BSYNC acknowledgement for the convergence ID; no concurrent scheduler is introduced.")
CONVERGENCE_ENTER_MODES = {
    mode: operation(f"convergence_enter_{mode.lower()}", CONVERGENCE_ENTER.arguments,
                    reads=CONVERGENCE_ENTER.reads, writes=CONVERGENCE_ENTER.writes,
                    description=f"Enter an explicit {mode} convergence generation. The current coordinator preserves the ordinary BSSY membership and ID-reuse rules for this encoded mode.")
    for mode in ("RELIABLE", "RECONVERGENT")}
CONVERGENCE_ARRIVE_MODES = {
    mode: operation(f"convergence_arrive_{mode.lower()}", CONVERGENCE_ARRIVE.arguments,
                    CONVERGENCE_ARRIVE.results, reads=CONVERGENCE_ARRIVE.reads, writes=CONVERGENCE_ARRIVE.writes,
                    description=f"Arrive at an explicit {mode} convergence ID. The current coordinator preserves the ordinary BSYNC membership rules for this encoded mode.")
    for mode in CONVERGENCE_ENTER_MODES}
CONVERGENCE_ACK_ENTER_MODE = operation("convergence_ack_enter_mode", (32, 32, 64), reads=("environment",),
                                      writes=("events",),
                                      description="Request standalone BSSY acknowledgement with the explicitly encoded reliability mode, convergence ID and target.")
CONVERGENCE_ACK_WAIT_MODE = operation("convergence_ack_wait_mode", (32, 32), reads=("environment",),
                                     writes=("events",),
                                     description="Request standalone BSYNC acknowledgement with the explicitly encoded reliability mode and convergence ID.")
CONVERGENCE_BREAK = operation("convergence_break", (32,), reads=("environment",),
                             writes=("convergence", "events"),
                             description="Request the standalone environment to break from the explicit convergence ID.")
CONVERGENCE_READ = operation("convergence_state_read", (32,), (32,), reads=("environment",),
                            writes=("events",), description="Read the explicit convergence-state selector from the standalone environment.")
CONVERGENCE_READ_CLEAR = operation("convergence_state_read_clear", (32,), (32,), reads=("environment",),
                                  writes=("convergence", "events"),
                                  description="Read and clear the explicit convergence-state selector in the standalone environment.")
CONVERGENCE_COPY = operation("convergence_barrier_copy", (32, 32), reads=("environment",),
                            writes=("convergence", "events"),
                            description="Copy the explicit source convergence barrier into the explicit destination barrier in the standalone environment.")
CONVERGENCE_IMPORT = operation("convergence_state_import", (32, 32), reads=("environment",),
                              writes=("convergence", "events"),
                              description="Import the explicit convergence-state selector into the explicit destination barrier in the standalone environment.")
CONVERGENCE_EXIT_PC = operation("convergence_exit_pc_write", (64,), reads=("environment",),
                               writes=("convergence", "events"),
                               description="Write the captured 64-bit exit PC in the standalone environment.")
CONVERGENCE_WRITE = operation("convergence_state_write", (32, 32, 32), reads=("environment",),
                             writes=("convergence", "events"),
                             description="Write the captured value to the explicit convergence-state selector with the explicit predicate-quad mode in the standalone environment.")
CONVERGENCE_WRITE_BARRIER = operation("convergence_barrier_write", (32, 32, 32), reads=("environment",),
                                     writes=("convergence", "events"),
                                     description="Write the explicit source barrier to the explicit convergence-state selector with the explicit predicate-quad mode in the standalone environment.")
DEPENDENCIES = operation("dependency_wait", (6,), reads=("dependencies", "async"), writes=("async", "shared"),
                         runtime="sass_dependency_wait",
                         description="Complete work in the explicitly encoded dependency slots synchronously in the CPU model.")
FENCE = operation("memory_fence", (32, 32), writes=("memory_order",), runtime="sass_fence",
                  description="Apply the explicitly selected fence semantics and memory scope.")
MEMORY_ORDER = tuple(operation(f"memory_order_{count}", (32,) * count, writes=("memory_order",),
                               runtime="sass_memory_order", description="Validate the class and its encoded memory controls.")
                     for count in range(1, 7))
DESCRIPTOR = operation("memory_descriptor", (64,), reads=("descriptor",), runtime="sass_memory_descriptor",
                       description="Validate the selected memory descriptor.")
PACKED_DESCRIPTOR = operation("packed_async_descriptor", (64, 1), reads=("descriptor",), runtime="sass_memory_descriptor",
                              description="Validate a packed asynchronous memory descriptor.")
CACHE = operation("cache_control", (32, 64), writes=("cache",), runtime="sass_cache_control",
                  description="Issue the selected data-cache request.")
LANE = operation("lane_id", results=(32,), reads=("warp",), runtime="sass_prim_lane",
                 description="Read the executing lane's index in its warp.")
ACTIVE = operation("active_lanes", results=(32,), reads=("warp",), runtime="sass_prim_active",
                   description="Read the mask of lanes executing this instruction.")
BARRIER_ELIGIBLE = operation("barrier_eligible_lanes", (32,), (32,), reads=("warp", "barrier"),
                             native="barrier_eligible_lanes",
                             description="Select lanes from the captured active mask whose earlier block-barrier obligations are ready. This query does not consume or execute their waits.")
WAITING = operation("waiting_lanes", results=(32,), reads=("warp",), runtime="sass_prim_waiting",
                    description="Read the mask of live lanes waiting elsewhere in this warp.")
ADDRESS_SPACE = operation("address_space", (64,), (32,), reads=("address_ranges",), runtime="sass_prim_address_space",
                         description="Classify a generic address using the environment's memory ranges.")
BARRIER_RESULT = operation("barrier_result", results=(32,), reads=("barrier",), runtime="sass_prim_bar_result",
                          description="Read the completed barrier's reduction result.")
BARRIER_STATE = operation("barrier_state", (32,), (32,), reads=("barrier",), runtime="sass_prim_bar_state",
                         description="Read the explicitly selected barrier's state.")
ASYNC_ISSUE = operation("async_issue", (64, 64, 64, 32, 1), reads=("async",), writes=("async",),
                       runtime="sass_async_issue", description="Enqueue captured bytes for asynchronous shared-memory copying.")
ASYNC_COMMIT = operation("async_commit", (32,), writes=("async",), runtime="sass_async_commit",
                        description="Commit the current asynchronous copy group to the specified dependency slot.")
ASYNC_WAIT_GROUPS = operation("async_wait_groups", (32, 32), reads=("async",), writes=("async", "shared"),
                             runtime="sass_async_wait_groups",
                             description="Complete the oldest copy groups in a dependency slot until at most the specified count remains.")
ASYNC_WAIT_SLOTS = operation("async_wait_slots", (6,), reads=("async",), writes=("async", "shared"),
                            runtime="sass_async_wait_slots",
                            description="Complete all committed copy groups in the selected dependency slots.")


def bound_operation(name, arguments):
    types = tuple(value_type(argument) for argument in arguments)
    matches = [spec for spec in OPERATIONS.values() if spec.runtime == name and spec.arguments == types]
    if len(matches) != 1: raise TypeError(f"no unique operation signature for {name}{types}")
    return matches[0]


@dataclass(frozen=True)
class DecodedRegister:
    name: str


@dataclass(frozen=True, eq=False)
class Move:
    target: object
    value: object


@dataclass(frozen=True, eq=False)
class ReadRegister:
    target: object
    location: tuple


@dataclass(frozen=True, eq=False)
class WriteRegister:
    location: tuple
    value: object


@dataclass(frozen=True, eq=False)
class ReadMemory:
    target: object
    space: str
    address: object


@dataclass(frozen=True, eq=False)
class WriteMemory:
    space: str
    address: object
    value: object


@dataclass(frozen=True, eq=False)
class Special:
    operation: Operation
    arguments: tuple = ()
    results: tuple = ()


@dataclass(frozen=True, eq=False)
class Block:
    statements: tuple = ()
    results: tuple = ()


@dataclass(frozen=True, eq=False)
class If:
    condition: object
    yes: Block
    no: Block = Block()
    results: tuple = ()


@dataclass(frozen=True, eq=False)
class Atomic:
    body: Block


@dataclass(frozen=True, eq=False)
class Transfer:
    kind: str
    target: object


def temp(index, type):
    return Handle(index, type) if isinstance(type, Opaque) else bitvec.Expr("temp", (index,), type)


def temp_index(value):
    if isinstance(value, Handle): return value.index
    if isinstance(value, bitvec.Expr) and value.op == "temp": return value.args[0]
    raise TypeError("expected a temporary")


class Builder:
    def __init__(self, start=0):
        self.count, self.statements = start, []

    def temp(self, type):
        value = temp(self.count, type)
        self.count += 1
        return value

    def emit(self, statement):
        self.statements.append(statement)
        return statement

    def move(self, value):
        target = self.temp(value_type(value))
        self.emit(Move(target, value))
        return target

    def read_register(self, file, index, count=1):
        target = self.temp(count * ELEMENT[file])
        self.emit(ReadRegister(target, (file, index, count)))
        return target

    def read_memory(self, space, address, width):
        target = self.temp(width)
        self.emit(ReadMemory(target, space, address))
        return target

    def special(self, spec, *arguments):
        results = tuple(self.temp(type) for type in spec.results)
        self.emit(Special(spec, tuple(arguments), results))
        return results

    def block(self, *results):
        return Block(tuple(self.statements), tuple(results))

    def if_else(self, condition, yes, no):
        condition = self.capture(condition)
        a = Builder(self.count)
        x = tuple(yes(a))
        b = Builder(a.count)
        y = tuple(no(b))
        if tuple(map(value_type, x)) != tuple(map(value_type, y)): raise TypeError("branch result types disagree")
        self.count = b.count
        results = tuple(self.temp(value_type(value)) for value in x)
        self.emit(If(condition, a.block(*x), b.block(*y), results))
        return results

    def assign_from_inputs(self, writes):
        writes = tuple(writes)
        captured = self.capture_inputs(value for _, value in writes)
        for (location, _), value in zip(writes, captured): self.emit(WriteRegister(location, value))

    def capture_inputs(self, expressions, memo=None):
        from collections import Counter
        expressions = tuple(expressions)
        def nodes(expression):
            seen, pending = {}, [expression]
            while pending:
                node = pending.pop()
                if not isinstance(node, bitvec.Expr) or node in seen: continue
                seen[node] = None
                match node.op:
                    case "ite": pending.append(node.args[0])
                    case "sel": pass
                    case _: pending.extend(reversed(node.args))
            return seen.keys()
        counts = Counter(node for expression in expressions for node in nodes(expression))
        shared = [node for node, count in counts.items() if count > 1 and
                  node.op not in ("const", "param", "reg", "load", "temp") and not constant_choice(node)]
        covered = set().union(*(set(nodes(node)) - {node} for node in shared)) if shared else set()
        shared = [node for node in shared if node not in covered]
        memo = {} if memo is None else memo
        aliases = {}
        def capture(value):
            source = self.capture(value, memo)
            reachable = nodes(source)
            for node in shared:
                operand = memo.get(node)
                if node is value or operand not in reachable or operand.op in ("const", "param", "temp"): continue
                target = self.move(operand)
                aliases[operand] = memo[node] = target
            source = bitvec.lowered(source, dict(aliases), preserve=bitvec.DEFINED)
            target = self.move(source)
            if not constant_choice(value): memo[value] = target
            return target
        return tuple(map(capture, expressions))
    def capture(self, expression, memo=None):
        memo = {} if memo is None else memo
        if not isinstance(expression, bitvec.Expr): return expression
        if expression in memo: return memo[expression]
        original = expression
        if expression.op in bitvec.DEFINED:
            inputs = definition_properties(*definition_signature(expression))[1]
            arguments = tuple(bitvec.const(0, argument.w) if isinstance(argument, bitvec.Expr) and
                              not constant_choice(argument) and index not in inputs else argument
                              for index, argument in enumerate(expression.args))
            expression = bitvec.rebuilt(expression, arguments)
        match expression.op:
            case "reg": value = self.read_register(*expression.args)
            case "load":
                space, address = expression.args
                value = self.read_memory(space, self.capture(address, memo), expression.w)
            case "primitive": raise TypeError("use Special for environment access")
            case "sel":
                inputs = capture_inputs(expression)
                if inputs is None and accesses_state(expression):
                    raise TypeError("stateful decode-time choice needs separate ordered bodies")
                for source in inputs or (): self.capture(source, memo)
                keys, cases = expression.args
                value = bitvec.Expr("sel", (keys, tuple((selections, self.capture(arm, memo))
                                                      for selections, arm in cases)), expression.w)
            case "ite" if accesses_state(expression.args[1]) or accesses_state(expression.args[2]):
                condition, yes, no = expression.args
                condition = self.capture(condition, memo)
                target = self.temp(expression.w)
                a = Builder(self.count)
                x = a.capture(yes, dict(memo))
                b = Builder(a.count)
                y = b.capture(no, dict(memo))
                self.count = b.count
                self.emit(If(condition, a.block(x), b.block(y), (target,)))
                value = target
            case op if op in bitvec.DEFINED and definition_accesses_state(expression):
                value = self.capture(bitvec.lowered(expression), memo)
            case _:
                value = bitvec.rebuilt(expression, [self.capture(arg, memo) for arg in expression.args])
        memo[original] = value
        return value


ELEMENT = {"R": 32, "UR": 32, "P": 1, "UP": 1, "SR": 32}


def location_type(location):
    file, index, count = location
    if file not in ELEMENT or type(count) is not int or count <= 0:
        raise TypeError(f"invalid register location: {location}")
    if isinstance(index, DecodedRegister):
        if not index.name: raise TypeError("empty decoded register field")
        return count * ELEMENT[file]
    if type(index) is not int or index < 0: raise TypeError(f"invalid register location: {location}")
    if index + count > (8 if file in ("P", "UP") else 256): raise TypeError("register span outside its file")
    return count * ELEMENT[file]


def validate(body, parameters=None):
    if body.results: raise TypeError("instruction body cannot return block results")
    defined = {}
    parameters = parameters or {}

    def expression(value, scope, expected=None):
        match value:
            case Handle():
                if scope.get(value.index) != value.type: raise TypeError("undefined opaque temporary")
                type_ = value.type
            case bitvec.Expr(op="temp"):
                if scope.get(value.args[0]) != value.w: raise TypeError("undefined temporary")
                type_ = value.w
            case bitvec.Expr():
                if type(value.w) is not int or value.w <= 0: raise TypeError("invalid expression width")
                match value.op:
                    case "reg" | "load" | "primitive": raise TypeError("state access must be a statement")
                    case "param":
                        if parameters.get(value.args[0]) != value.w: raise TypeError("unbound parameter")
                    case "const":
                        if len(value.args) != 1 or type(value.args[0]) is not int or not 0 <= value.args[0] < 1 << value.w:
                            raise TypeError("invalid constant")
                    case "sel":
                        if len(value.args) != 2: raise TypeError("invalid decode-time choice")
                        keys, cases = value.args
                        if not keys or not cases: raise TypeError("empty decode-time choice")
                        for selections, arm in cases:
                            if len(selections) != len(keys): raise TypeError("decode-time choice key mismatch")
                            expression(arm, scope, value.w)
                    case op if op in bitvec.OPS or op in bitvec.DEFINED or op == "ite":
                        for argument in value.args:
                            if isinstance(argument, (bitvec.Expr, Handle)): expression(argument, scope)
                        check_expression(value)
                    case _: raise TypeError(f"unknown expression operator: {value.op}")
                type_ = value.w
            case _: raise TypeError("expected a typed value")
        if expected is not None and type_ != expected: raise TypeError(f"type mismatch: {type_} != {expected}")
        return type_

    def define(target, scope, type_):
        index = temp_index(target)
        if type(index) is not int or index < 0: raise TypeError("invalid temporary index")
        if not isinstance(type_, Opaque) and (type(type_) is not int or type_ <= 0):
            raise TypeError("invalid temporary type")
        if index in defined: raise TypeError("temporary defined twice")
        if value_type(target) != type_: raise TypeError("temporary type mismatch")
        defined[index] = type_
        scope[index] = type_

    def memory(space, address, width, scope):
        if space not in ("ram", "shared", "localmem", "cbank"): raise TypeError("unknown memory space")
        if type(width) is not int or width <= 0 or width % 8:
            raise TypeError("memory width must be a positive multiple of eight")
        expression(address, scope, 64)

    def block(body, incoming, atomic=False):
        scope = dict(incoming)
        terminal = False
        for statement in body.statements:
            if terminal: raise TypeError("statement after terminal control transfer")
            match statement:
                case Move(target, value): define(target, scope, expression(value, scope))
                case ReadRegister(target, location): define(target, scope, location_type(location))
                case WriteRegister(location, value): expression(value, scope, location_type(location))
                case ReadMemory(target, space, address):
                    memory(space, address, value_type(target), scope)
                    define(target, scope, value_type(target))
                case WriteMemory(space, address, value):
                    memory(space, address, expression(value, scope), scope)
                case Special(spec, arguments, results):
                    if atomic and spec.suspends: raise TypeError("suspension inside atomic block")
                    if OPERATIONS.get(spec.name) != spec: raise TypeError("unregistered operation")
                    if len(arguments) != len(spec.arguments) or len(results) != len(spec.results):
                        raise TypeError("operation arity mismatch")
                    for argument, type_ in zip(arguments, spec.arguments): expression(argument, scope, type_)
                    for target, type_ in zip(results, spec.results): define(target, scope, type_)
                case If(condition, yes, no, results):
                    expression(condition, scope, 1)
                    a, ta = block(yes, scope, atomic)
                    b, tb = block(no, scope, atomic)
                    if len(a) != len(b) or len(a) != len(results): raise TypeError("branch result arity mismatch")
                    for target, x, y in zip(results, a, b):
                        if x != y: raise TypeError("branch result type mismatch")
                        define(target, scope, x)
                    if results and (ta or tb): raise TypeError("terminal branch cannot return values")
                    terminal = ta and tb
                case Atomic(inner):
                    if atomic: raise TypeError("nested atomic block")
                    results, _ = block(inner, scope, True)
                    if results: raise TypeError("atomic block cannot return values")
                case Transfer(kind, target):
                    if atomic: raise TypeError("control transfer inside atomic block")
                    if kind not in ("goto", "call", "return"): raise TypeError("unknown control transfer")
                    expression(target, scope, 64)
                    terminal = True
                case _: raise TypeError(f"unknown statement: {statement}")
        if terminal and body.results: raise TypeError("terminal block cannot return values")
        return tuple(expression(value, scope) for value in body.results), terminal

    block(body, {})
    return defined


@dataclass(frozen=True, eq=False)
class Branch:
    condition: object
    yes: int
    no: int


@dataclass(frozen=True)
class Jump:
    target: int


@dataclass(frozen=True)
class Boundary:
    enter: bool


def linearize(body):
    code = []

    def block(body):
        for statement in body.statements:
            match statement:
                case If(condition, yes, no, results):
                    branch = len(code)
                    code.append(None)
                    if not results and (not yes.statements or not no.statements):
                        arm = len(code)
                        block(yes if yes.statements else no)
                        code[branch] = Branch(condition, arm, len(code)) if yes.statements else Branch(condition, len(code), arm)
                        continue
                    a = len(code)
                    block(yes)
                    code.extend(Move(target, value) for target, value in zip(results, yes.results))
                    jump = len(code)
                    code.append(None)
                    b = len(code)
                    block(no)
                    code.extend(Move(target, value) for target, value in zip(results, no.results))
                    code[branch], code[jump] = Branch(condition, a, b), Jump(len(code))
                case Atomic(inner):
                    code.append(Boundary(True))
                    block(inner)
                    code.append(Boundary(False))
                case _: code.append(statement)
    block(body)
    return tuple(code)


def map_values(body, function):
    statements = []
    for statement in body.statements:
        match statement:
            case Move(target, value): result = Move(function(target), function(value))
            case ReadRegister(target, location): result = ReadRegister(function(target), location)
            case WriteRegister(location, value): result = WriteRegister(location, function(value))
            case ReadMemory(target, space, address): result = ReadMemory(function(target), space, function(address))
            case WriteMemory(space, address, value): result = WriteMemory(space, function(address), function(value))
            case Special(spec, arguments, results):
                result = Special(spec, tuple(map(function, arguments)), tuple(map(function, results)))
            case If(condition, yes, no, results):
                result = If(function(condition), map_values(yes, function), map_values(no, function),
                            tuple(map(function, results)))
            case Atomic(inner): result = Atomic(map_values(inner, function))
            case Transfer(kind, target): result = Transfer(kind, function(target))
            case _: raise TypeError(f"unknown statement: {statement}")
        statements.append(result)
    return Block(tuple(statements), tuple(map(function, body.results)))


def shifted(body, offset):
    def value(expression):
        if isinstance(expression, Handle): return Handle(expression.index + offset, expression.type)
        return bitvec.substitute(expression, lambda e: temp(e.args[0] + offset, e.w) if e.op == "temp" else None)
    return map_values(body, value)


@lru_cache(maxsize=2048)
def prepare(body, parameters=()):
    return validate(body, dict(parameters)), linearize(body)


def statement_values(statement):
    match statement:
        case Move(_, value) | WriteRegister(_, value) | Branch(value, _, _) | Transfer(_, value): return (value,)
        case ReadMemory(_, _, address): return (address,)
        case WriteMemory(_, address, value): return address, value
        case Special(_, arguments): return arguments
    return ()


def parameter_types(body):
    types = {}
    for statement in linearize(body):
        for value in statement_values(statement):
            if isinstance(value, Handle): continue
            for parameter in bitvec.leaves(value, "param"):
                name, = parameter.args
                if types.setdefault(name, parameter.w) != parameter.w:
                    raise TypeError(f"parameter {name} used at widths {types[name]} and {parameter.w}")
    return types


@lru_cache(maxsize=2048)
def written_locations(body):
    return tuple(dict.fromkeys(statement.location for statement in linearize(body)
                               if isinstance(statement, WriteRegister)))


def retained_temporaries(code):
    definitions, uses = {}, {}
    pauses = []
    for position, statement in enumerate(code):
        targets = (statement.target,) if isinstance(statement, (Move, ReadRegister, ReadMemory)) else \
                  statement.results if isinstance(statement, Special) else ()
        for target in targets: definitions.setdefault(temp_index(target), position)
        for value in statement_values(statement):
            indices = [value.index] if isinstance(value, Handle) else [e.args[0] for e in bitvec.leaves(value, "temp")]
            for index in indices: uses.setdefault(index, []).append(position)
        if isinstance(statement, Special) and statement.operation.suspends: pauses.append(position)
    return frozenset(index for index, definition in definitions.items()
                     if any(definition < pause and any(use > pause for use in uses.get(index, ())) for pause in pauses))


class Status(Enum):
    COMPLETE = "complete"
    SUSPENDED = "suspended"
    FAULT = "fault"


@dataclass
class Pending:
    poll: object


@dataclass
class Frame:
    body: Block
    next_pc: int
    parameters: dict = field(default_factory=dict)
    position: int = 0
    values: dict = field(default_factory=dict)
    pending: Pending | None = None
    atomic: bool = False
    status: Status | None = None
    error: Exception | None = None

    def __post_init__(self):
        _, self.code = prepare(self.body, tuple(sorted((name, value.w) for name, value in self.parameters.items())))
        self.memo = {}
        self.params = {name: bitvec.value(value) for name, value in self.parameters.items()}

    def run(self, state, runtime=None):
        from sass.semantics import evaluate

        def value(expr):
            if isinstance(expr, Handle): return self.values[expr.index]
            if expr.op == "temp": return self.values[expr.args[0]] & bitvec.mask(expr.w)
            if expr.op == "const": return expr.args[0]
            return evaluate.evaluate(expr, None, self.params, self.memo, temporaries=self.values)

        def results(statement, values):
            if not isinstance(values, tuple) or len(values) != len(statement.results):
                raise TypeError("runtime result arity mismatch")
            for target, result in zip(statement.results, values):
                if type(result) is not int: raise TypeError("runtime result must be an integer or numeric handle")
                if isinstance(target, Handle): runtime.object(result, target.type)
                elif not 0 <= result < 1 << target.w: raise TypeError("runtime result outside its declared width")
                self.values[temp_index(target)] = result

        def release():
            if self.atomic:
                self.atomic = False
                runtime.release(self) if runtime is not None else None

        if self.status in (Status.COMPLETE, Status.FAULT): return self.status
        try:
            while self.position < len(self.code):
                statement = self.code[self.position]
                match statement:
                    case Move(target, expr): self.values[temp_index(target)] = value(expr)
                    case ReadRegister(target, location): self.values[temp_index(target)] = state.read(*location)
                    case WriteRegister(location, expr): state.write(*location, value(expr))
                    case ReadMemory(target, space, address): self.values[temp_index(target)] = state.load(space, value(address), target.w)
                    case WriteMemory(space, address, expr): state.store(space, value(address), expr.w, value(expr))
                    case Special(spec, arguments):
                        if runtime is None: raise evaluate.Unknown(f"operation {spec.name} without a runtime")
                        if self.pending is None:
                            reply = runtime.call(spec.name, tuple(map(value, arguments)))
                            if isinstance(reply, Pending):
                                if not spec.suspends: raise TypeError("non-suspending operation returned a pending result")
                                self.pending = reply
                            else: results(statement, reply)
                        if self.pending is not None:
                            reply = self.pending.poll()
                            if reply is None:
                                self.status = Status.SUSPENDED
                                return self.status
                            results(statement, reply)
                            self.pending = None
                    case Branch(condition, yes, no):
                        self.position = yes if value(condition) else no
                        continue
                    case Jump(target):
                        self.position = target
                        continue
                    case Boundary(enter=True):
                        if runtime is not None: runtime.acquire(self)
                        self.atomic = True
                    case Boundary(enter=False): release()
                    case Transfer(_, target):
                        self.next_pc = value(target)
                        self.position = len(self.code)
                        break
                self.position += 1
            state.pc = self.next_pc
            self.status = Status.COMPLETE
        except Exception as error:
            release()
            self.pending = None
            self.error, self.status = error, Status.FAULT
        return self.status


def check_expression(expression):
    op, args, width = expression.op, expression.args, expression.w
    if op in bitvec.DEFINED:
        if definition_accesses_state(expression):
            raise TypeError("state access inside a numerical definition")
        return
    unary = {"not", "neg", "lzcount", "zext", "sext", "fnan"}
    binary = {"add", "sub", "mul", "and", "or", "xor", "shl", "shr", "sar", "eq", "ne",
              "ult", "ule", "slt", "sle", "flt", "fle", "feq", "nanresult"}
    arity = 1 if op in unary else 2 if op in binary else {
        "ite": 3, "extract": 3, "fadd": 3, "fmul": 4, "ffma": 4, "fconv": 2}.get(op)
    if arity is not None and len(args) != arity: raise TypeError("expression argument count mismatch")
    count = 1 if op in unary else 2 if op in binary else {
        "ite": 3, "extract": 1, "fadd": 2, "fmul": 2, "ffma": 3, "fconv": 1}.get(op)
    values = tuple(arg for arg in args if isinstance(arg, bitvec.Expr))
    if any(isinstance(arg, Handle) for arg in args): raise TypeError("opaque value in a numerical expression")
    if count is not None and len(values) != count: raise TypeError("expression operand count mismatch")
    if op in binary - {"shl", "shr", "sar"} and args[0].w != args[1].w:
        raise TypeError("expression operand width mismatch")
    if op in {"eq", "ne", "ult", "ule", "slt", "sle", "flt", "fle", "feq", "fnan"} and width != 1:
        raise TypeError("comparison must return one bit")
    if op in {"add", "sub", "mul", "and", "or", "xor", "not", "neg", "shl", "shr", "sar", "nanresult"} and args[0].w != width:
        raise TypeError("expression result width mismatch")
    match op:
        case "mufu":
            from sass.semantics import primitive
            if (len(args) not in (2, 3) or not isinstance(args[0], str) or
                args[0] not in tuple(name.lower() for name in primitive.MUFU) or
                not isinstance(args[1], bitvec.Expr) or args[1].w != width or
                width not in (16, 32) or (width == 16) != (len(args) == 3) or
                len(args) == 3 and (not isinstance(args[2], bitvec.Expr) or args[2].w != 1)):
                raise TypeError("invalid MUFU signature")
        case "ite" if args[0].w != 1 or args[1].w != width or args[2].w != width:
            raise TypeError("invalid conditional expression")
        case "zext" | "sext" if args[0].w > width: raise TypeError("extension cannot narrow")
        case "extract" if len(args) != 3 or not 0 <= args[2] <= args[1] < args[0].w or args[1] - args[2] + 1 != width:
            raise TypeError("invalid extract")
        case "concat" if len(values) != len(args) or not values or sum(arg.w for arg in values) != width:
            raise TypeError("invalid concat")
        case "fadd" | "fmul" | "ffma":
            count = 3 if op == "ffma" else 2
            if width not in (16, 32, 64) or any(arg.w != width for arg in values) or args[count] not in bitvec.ROUNDING:
                raise TypeError("invalid floating point expression")
        case "fconv" if width not in (16, 32, 64) or args[0].w not in (16, 32, 64) or args[1] not in bitvec.ROUNDING:
            raise TypeError("invalid floating point conversion")
