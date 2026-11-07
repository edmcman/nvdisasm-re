from dataclasses import dataclass, field
from sass.semantics import ordered


SNAPSHOT_PRIMITIVES = {ordered.BALLOT.name: "ballot", ordered.MATCH.name: "match", ordered.MATCH64.name: "match",
                      **{spec.name: f"redux_{name}" for name, spec in ordered.REDUCTIONS.items()}}
ACKNOWLEDGED_BARRIER = object()
CONVERGENCE_OPERATIONS = frozenset((
    ordered.CONVERGENCE_ENTER.name, ordered.CONVERGENCE_ARRIVE.name, ordered.CONVERGENCE_WAIT.name,
    *(spec.name for spec in ordered.CONVERGENCE_ENTER_MODES.values()),
    *(spec.name for spec in ordered.CONVERGENCE_ARRIVE_MODES.values())))
ACKNOWLEDGEMENTS = {spec.name: op for op, specs in (("bar", ordered.BARRIER_ACKS), ("warpsync", ordered.WARP_ACKS))
                    for spec in specs.values()}


class Runtime:
    def __init__(self, handlers=None, provider=None):
        self.handlers = handlers or {}
        self.provider = provider
        self.objects = {}
        self.serial = 0
        self.owner = None

    def retain(self, type_, value):
        self.serial += 1
        if self.serial >= 1 << 64: raise OverflowError("opaque handle space exhausted")
        self.objects[self.serial] = type_, value
        return self.serial

    def object(self, handle, type_):
        if handle not in self.objects or self.objects[handle][0] != type_: raise TypeError("invalid opaque handle")
        return self.objects[handle][1]

    def release_handle(self, handle):
        if handle not in self.objects: raise TypeError("invalid opaque handle")
        del self.objects[handle]

    def close(self):
        if self.owner is not None: raise RuntimeError("closing runtime with an active atomic block")
        self.objects.clear()

    def acquire(self, frame):
        if self.owner is not None: raise RuntimeError("atomic block already owned")
        self.owner = frame

    def release(self, frame):
        if self.owner is not frame: raise RuntimeError("atomic block owner mismatch")
        self.owner = None

    def call(self, name, arguments):
        spec = ordered.OPERATIONS[name]
        if len(arguments) != len(spec.arguments): raise TypeError("runtime argument arity mismatch")
        for argument, type_ in zip(arguments, spec.arguments):
            if isinstance(type_, ordered.Opaque): self.object(argument, type_)
            elif type(argument) is not int or not 0 <= argument < 1 << type_: raise TypeError("invalid runtime argument")
        if name in (ordered.RELEASE_SNAPSHOT.name, ordered.RELEASE_SNAPSHOT64.name, ordered.RELEASE_WORDS.name,
                    ordered.RELEASE_TICKET.name, ordered.BLOCK_RELEASE.name, ordered.WARP_RELEASE.name,
                    ordered.CONVERGENCE_RELEASE.name):
            self.release_handle(arguments[0])
            return ()
        if name in (ordered.READ_LANE.name, ordered.READ_LANE64.name):
            return self.read_lane(arguments[0], arguments[1], spec.arguments[0])
        if name == ordered.READ_WORD.name:
            from sass.semantics.runtime import UnsupportedSemantics
            handle, lane, word = arguments
            words, = self.read_lane(handle, lane, ordered.WORD_SNAPSHOT)
            if word >= len(words): raise UnsupportedSemantics("snapshot word is out of range")
            return words[word],
        if name in SNAPSHOT_PRIMITIVES:
            from sass.emulate import native
            from sass.semantics.runtime import UnsupportedSemantics
            values = self.object(arguments[0], spec.arguments[0])
            mask = sum(1 << lane for lane in values)
            lane = arguments[1] if len(arguments) > 1 else min(values)
            if lane not in values: raise UnsupportedSemantics("snapshot source is inactive")
            return native.primitive(SNAPSHOT_PRIMITIVES[name], [[values.get(index, 0) for index in range(32)]], lane, mask),
        if name in self.handlers: return self.handlers[name](*arguments)
        if name in ACKNOWLEDGEMENTS:
            from sass.semantics.runtime import UnsupportedSemantics
            if self.provider is None: raise UnsupportedSemantics("synchronization context required")
            self.provider.synchronize(ACKNOWLEDGEMENTS[name], [1, *arguments], read=False)
            return ()
        if name == ordered.COORDINATED.name:
            return int(self.provider is not None and self.provider.context.get("coordination") is not None),
        if name == ordered.BARRIER_ELIGIBLE.name:
            state = self.provider.context.get("coordination") if self.provider is not None else None
            return (state.eligible_lanes(arguments[0]) if state is not None else arguments[0]),
        if name in (ordered.CONVERGENCE_BREAK.name, ordered.CONVERGENCE_READ.name,
                    ordered.CONVERGENCE_READ_CLEAR.name, ordered.CONVERGENCE_COPY.name,
                    ordered.CONVERGENCE_IMPORT.name, ordered.CONVERGENCE_EXIT_PC.name,
                    ordered.CONVERGENCE_WRITE.name, ordered.CONVERGENCE_WRITE_BARRIER.name):
            from sass.semantics.runtime import UnsupportedSemantics
            if self.provider is None: raise UnsupportedSemantics("convergence state environment required")
            read = name in (ordered.CONVERGENCE_READ.name, ordered.CONVERGENCE_READ_CLEAR.name)
            inputs = [*arguments, int(name == ordered.CONVERGENCE_READ_CLEAR.name)] if read else [1, *arguments]
            result = self.provider.synchronize("break" if name == ordered.CONVERGENCE_BREAK.name else "bmov", inputs, read)
            return (result,) if read else ()
        if name in (ordered.CONVERGENCE_ACK_ENTER.name, ordered.CONVERGENCE_ACK_ENTER_MODE.name,
                    ordered.CONVERGENCE_ACK_WAIT.name, ordered.CONVERGENCE_ACK_WAIT_MODE.name):
            from sass.semantics.runtime import UnsupportedSemantics
            enter = name in (ordered.CONVERGENCE_ACK_ENTER.name, ordered.CONVERGENCE_ACK_ENTER_MODE.name)
            if self.provider is None: raise UnsupportedSemantics("convergence acknowledgement context required")
            self.provider.synchronize("bssy" if enter else "bsync", [1, *arguments], read=False)
            return ()
        if name in CONVERGENCE_OPERATIONS:
            from sass.semantics.runtime import UnsupportedSemantics
            state = self.provider.context.get("coordination") if self.provider is not None else None
            if state is None: raise UnsupportedSemantics("cooperative convergence context required")
            match name:
                case "convergence_enter" | "convergence_enter_reliable" | "convergence_enter_reconvergent":
                    state.enter_convergence(*arguments)
                    return ()
                case "convergence_arrive" | "convergence_arrive_reliable" | "convergence_arrive_reconvergent":
                    return self.retain(ordered.CONVERGENCE_TICKET, state.arrive_convergence(*arguments)),
                case _:
                    ticket = self.object(arguments[0], ordered.CONVERGENCE_TICKET)
                    state.check_convergence(ticket)
                    return ordered.Pending(lambda: () if ticket.ready() else None)
        if name in (ordered.WARP_ARRIVE.name, ordered.WARP_ARRIVE_EXCLUSIVE.name, ordered.WARP_WAIT.name):
            from sass.semantics.runtime import UnsupportedSemantics
            state = self.provider.context.get("coordination") if self.provider is not None else None
            if name != ordered.WARP_WAIT.name:
                if name == ordered.WARP_ARRIVE_EXCLUSIVE.name and state is not None:
                    raise UnsupportedSemantics("unsupported exclusive warp synchronization")
                if state is None:
                    if self.provider is None: raise UnsupportedSemantics("WARPSYNC synchronization context required")
                    self.provider.synchronize("warpsync", [1, *arguments], read=False)
                    return self.retain(ordered.WARP_TICKET, ACKNOWLEDGED_BARRIER),
                return self.retain(ordered.WARP_TICKET, state.arrive_warp(*arguments)),
            ticket = self.object(arguments[0], ordered.WARP_TICKET)
            if ticket is ACKNOWLEDGED_BARRIER: return ()
            if state is None: raise UnsupportedSemantics("cooperative warp synchronization context required")
            state.check_warp(ticket)
            return ordered.Pending(lambda: () if ticket.ready() else None)
        if name.startswith("block_barrier_"):
            from sass.semantics.runtime import UnsupportedSemantics
            state = self.provider.context.get("coordination") if self.provider is not None else None
            match name:
                case "block_barrier_arrive_implicit":
                    if state is None:
                        if self.provider is None: raise UnsupportedSemantics("BAR synchronization context required")
                        self.provider.synchronize("bar", [1, *arguments], read=False)
                        return self.retain(ordered.BLOCK_TICKET, ACKNOWLEDGED_BARRIER),
                    return self.retain(ordered.BLOCK_TICKET, state.arrive(*arguments)),
                case "block_barrier_arrive_counted":
                    id, count, predicate, reduction = arguments
                    if state is None:
                        if self.provider is None: raise UnsupportedSemantics("BAR synchronization context required")
                        self.provider.synchronize("bar", [1, *arguments], read=False)
                        return self.retain(ordered.BLOCK_TICKET, ACKNOWLEDGED_BARRIER),
                    return self.retain(ordered.BLOCK_TICKET, state.arrive(id, predicate, reduction, count)),
                case "block_barrier_wait_ready" | "block_barrier_defer":
                    ticket = self.object(arguments[0], ordered.BLOCK_TICKET)
                    if ticket is ACKNOWLEDGED_BARRIER: return ()
                    if state is None: raise UnsupportedSemantics("cooperative barrier context required")
                    state.bind(ticket, deferred=name == "block_barrier_defer")
                    return () if name == "block_barrier_defer" else ordered.Pending(lambda: () if ticket.ready() else None)
                case "block_barrier_wait_deferred":
                    if state is None: return ()
                    ticket = state.pending()
                    if ticket is None: return ()
                    state.bind(ticket)
                    return ordered.Pending(lambda: () if ticket.ready() else None)
                case "block_barrier_result" | "block_barrier_state":
                    if state is not None: return state.read(*arguments),
                    if self.provider is None: raise UnsupportedSemantics("barrier environment required")
                    operation = "sass_prim_bar_result" if name == "block_barrier_result" else "sass_prim_bar_state"
                    return self.provider.call(operation, list(arguments), 4),
        if name == ordered.DEPENDENCIES.name and self.provider is None: return ()
        if spec.runtime and self.provider is not None:
            if len(spec.results) > 1: raise TypeError("bound runtime operation has multiple results")
            width = spec.results[0] if spec.results else 0
            result = self.provider.call(spec.runtime, list(arguments), (width + 7) // 8)
            return (result,) if spec.results else ()
        raise NotImplementedError(f"ordered operation {name}")

    def read_lane(self, snapshot, lane, type_=ordered.SNAPSHOT):
        from sass.semantics.runtime import UnsupportedSemantics
        values = self.object(snapshot, type_)
        if lane not in values: raise UnsupportedSemantics("snapshot source is inactive")
        return values[lane],


class Explicit(Runtime):
    def __init__(self, provider=None):
        super().__init__(provider=provider)
        self.handlers = {
            ordered.CAPTURE.name: self.capture,
            ordered.CAPTURE64.name: lambda lane, mask, value: self.capture(lane, mask, value, ordered.SNAPSHOT64),
            ordered.CAPTURE_PREDICATE.name: lambda lane, mask, value: self.capture(lane, mask, value, predicates=True),
            **{spec.name: lambda group, lane, mask, value, width=width: self.capture_words(group, lane, mask, value, width)
               for width, spec in ordered.CAPTURE_WORDS.items()},
        }

    def capture(self, lane, mask, value, type_=ordered.SNAPSHOT, predicates=False):
        from sass.semantics.runtime import UnsupportedSemantics
        if self.provider is None: raise UnsupportedSemantics("explicit 32-lane warp context required")
        _, current, active = self.provider.warp()
        if lane != current or not mask >> lane & 1 or mask & ~active:
            raise UnsupportedSemantics("invalid capture mask or lane")
        values = self.provider.lanes("predicates" if predicates else "values", value)
        bits = 1 if predicates else 64 if type_ == ordered.SNAPSHOT64 else 32
        return self.retain(type_, {index: values[index] & ((1 << bits) - 1)
                                  for index in range(32) if mask >> index & 1}),

    def capture_words(self, group, lane, mask, value, width):
        from sass.semantics import primitive
        from sass.semantics.runtime import UnsupportedSemantics
        if self.provider is None: raise UnsupportedSemantics("explicit 32-lane warp context required")
        warp, current, active = self.provider.warp()
        if lane != current or not mask >> lane & 1 or mask & ~active:
            raise UnsupportedSemantics("invalid capture mask or lane")
        if group >= len(primitive.OPERAND_GROUPS): raise UnsupportedSemantics("warp operand group is out of range")
        rows = warp.get("operand_groups", {}).get(primitive.OPERAND_GROUPS[group])
        if rows is None or len(rows) != 32: raise UnsupportedSemantics("warp operand group required")
        values = {}
        for index in range(32):
            if not mask >> index & 1: continue
            if len(rows[index]) < width // 32: raise UnsupportedSemantics("warp operand group is too short")
            values[index] = tuple(value >> bit & 0xffffffff for bit in range(0, width, 32)) if index == lane else \
                            tuple(int(word) & 0xffffffff for word in rows[index][:width // 32])
        return self.retain(ordered.WORD_SNAPSHOT, values),


@dataclass
class Barrier:
    count: int
    arrivals: dict = field(default_factory=dict)
    result: int | None = None


class Cooperative(Runtime):
    def __init__(self, provider=None):
        super().__init__(provider=provider)
        self.barriers, self.captures = {}, {}
        self.frame, self.lane, self.site, self.participant = None, None, None, None
        self.handlers = {
            ordered.CAPTURE.name: self.capture,
            ordered.CAPTURE64.name: lambda lane, mask, value: self.capture(lane, mask, value, ordered.SNAPSHOT64),
            ordered.CAPTURE_PREDICATE.name: self.capture,
            **{spec.name: lambda group, lane, mask, value, width=width: self.capture_words(group, lane, mask, value, width)
               for width, spec in ordered.CAPTURE_WORDS.items()},
            ordered.ARRIVE.name: self.arrive,
            ordered.WAIT.name: self.wait,
            ordered.WAIT_READY.name: self.wait_ready,
        }

    def bind(self, frame, lane, site=None, participant=None):
        if self.owner is not None and self.owner is not frame: raise RuntimeError("atomic block owned by another frame")
        self.frame, self.lane, self.site = frame, lane, site
        self.participant = lane if participant is None else participant

    def capture(self, lane, mask, value, type_=ordered.SNAPSHOT, signature=None):
        if lane != self.lane or lane >= 32 or not mask >> lane & 1: raise ValueError("capture mask omits the lane")
        position = getattr(self.frame, "position", None)
        key = (self.site, position) if self.site is not None else (self.frame.next_pc, position)
        group = self.captures.setdefault(key, dict(mask=mask, type=type_, signature=signature, values={}))
        if (group["mask"], group["type"], group["signature"]) != (mask, type_, signature):
            raise ValueError("capture masks, types, or groups disagree")
        if lane in group["values"]: raise ValueError("capture contribution repeats")
        group["values"][lane] = value
        if len(group["values"]) == mask.bit_count():
            del self.captures[key]

        handle = None
        def poll():
            nonlocal handle
            if len(group["values"]) != mask.bit_count(): return None
            if handle is None: handle = self.retain(type_, group["values"])
            return handle,
        return ordered.Pending(poll)

    def capture_words(self, group, lane, mask, value, width):
        from sass.semantics import primitive
        if group >= len(primitive.OPERAND_GROUPS): raise ValueError("warp operand group is out of range")
        words = tuple(value >> bit & 0xffffffff for bit in range(0, width, 32))
        return self.capture(lane, mask, words, ordered.WORD_SNAPSHOT, (group, width))

    def arrive(self, index, count, predicate):
        if index >= 16 or count <= 0: raise ValueError("invalid barrier")
        barrier = self.barriers.setdefault(index, Barrier(count))
        if barrier.count != count: raise ValueError("barrier participant counts disagree")
        participant = self.lane if self.participant is None else self.participant
        if participant in barrier.arrivals: raise ValueError("barrier arrival repeats before completion")
        barrier.arrivals[participant] = predicate
        if len(barrier.arrivals) == count:
            barrier.result = sum(barrier.arrivals.values())
            del self.barriers[index]
        return self.retain(ordered.TICKET, barrier),

    def wait(self, ticket):
        barrier = self.object(ticket, ordered.TICKET)
        return ordered.Pending(lambda: None if barrier.result is None else (barrier.result,))

    def wait_ready(self, ticket):
        pending = self.wait(ticket)
        return ordered.Pending(lambda: () if pending.poll() is not None else None)

    def close(self):
        super().close()
        self.captures.clear()
        self.barriers.clear()


def reference():
    header = "| Operation | Arguments | Results | Reads | Writes | Suspends | Description |\n|---|---|---|---|---|---|---|"
    names = lambda types: ", ".join(f"bits{t}" if isinstance(t, int) else t.name for t in types) or "—"
    rows = [f"| `{spec.name}` | {names(spec.arguments)} | {names(spec.results)} | "
            f"{', '.join(spec.reads) or '—'} | {', '.join(spec.writes) or '—'} | "
            f"{'yes' if spec.suspends else 'no'} | {spec.description} |"
            for spec in ordered.OPERATIONS.values()]
    return "\n".join([header, *rows])


if __name__ == "__main__":
    print("# Ordered runtime operations\n\nGenerated from `sass.semantics.ordered.OPERATIONS`.\n\n"
          "Arguments and results are bit vectors of the stated width or typed opaque handles.\n\n" + reference())
