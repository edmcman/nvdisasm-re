from functools import cache
from sass.semantics import ordered


PREFIX = "sass_ordered_"
RECORD = ordered.Opaque("result_record")


@cache
def advance():
    from sass.ghidra_bridge import ghidra_decode
    return ghidra_decode.java_class("BlockEmulator").advance


class Execution:
    def __init__(self, thread, runtime):
        self.thread, self.runtime = thread, runtime
        self.step_instruction = None
        self.pending = None
        self.output = None
        self.atomic = False
        self.error = None
        self.status = None

    @property
    def position(self):
        frame = self.thread.getFrame() if self.thread is not None else None
        return int(frame.index()) if frame is not None else None

    def result(self, name, values):
        spec = ordered.OPERATIONS[name.removeprefix(PREFIX)]
        if not isinstance(values, tuple) or len(values) != len(spec.results): raise TypeError("runtime result arity mismatch")
        for value, type_ in zip(values, spec.results):
            if type(value) is not int: raise TypeError("runtime result must be numeric")
            if isinstance(type_, ordered.Opaque): self.runtime.object(value, type_)
            elif not 0 <= value < 1 << type_: raise TypeError("runtime result outside its declared width")
        return self.runtime.retain(RECORD, values) if len(values) > 1 else values[0] if values else 0

    def call(self, name, arguments, output):
        match name.removeprefix(PREFIX):
            case "atomic_enter":
                self.runtime.acquire(self)
                self.atomic = True
                return 0
            case "atomic_exit":
                self.release()
                return 0
            case "release_record":
                self.runtime.object(arguments[0], RECORD)
                self.runtime.release_handle(arguments[0])
                return 0
            case projection if projection.startswith("result_"):
                return self.runtime.object(arguments[0], RECORD)[arguments[1]]
        spec = ordered.OPERATIONS[name.removeprefix(PREFIX)]
        if self.atomic and spec.suspends: raise TypeError("suspension inside atomic block")
        values = self.runtime.call(spec.name, tuple(arguments))
        if isinstance(values, ordered.Pending):
            if not spec.suspends: raise TypeError("non-suspending operation suspended")
            values = self.poll_or_retain(values, name, output)
        return self.result(name, values)

    def poll_or_retain(self, pending, name, output):
        values = pending.poll()
        if values is not None: return values
        from ghidra.pcode.exec import SuspendedPcodeExecutionException
        self.pending, self.output = (pending, name), output
        raise SuspendedPcodeExecutionException(None, None)

    def write(self, output, value):
        if output is None: return
        import jpype
        size = int(output.getSize())
        value &= (1 << (size * 8)) - 1
        self.thread.getState().setVar(output, jpype.JArray(jpype.JByte)(value.to_bytes(size, "little")))

    def release(self):
        if self.atomic:
            self.atomic = False
            self.runtime.release(self)

    def step(self):
        if self.status == ordered.Status.COMPLETE: return self.status
        if self.error is not None: return ordered.Status.FAULT
        try:
            if self.pending is not None:
                pending, name = self.pending
                self.pending = None
                values = pending.poll()
                if values is None:
                    self.pending = pending, name
                    return ordered.Status.SUSPENDED
                self.write(self.output, self.result(name, values))
                self.pending = self.output = None
            if self.step_instruction is None: self.step_instruction = advance()
            if not self.step_instruction(self.thread):
                if self.pending is None: raise RuntimeError("suspended p-code has no pending operation")
                return ordered.Status.SUSPENDED
            self.status = ordered.Status.COMPLETE
            return ordered.Status.COMPLETE
        except Exception as error:
            if self.pending is not None: return ordered.Status.SUSPENDED
            self.output = None
            self.error = error
            self.release()
            return ordered.Status.FAULT
