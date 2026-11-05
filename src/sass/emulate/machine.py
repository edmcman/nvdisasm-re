"""The machine of include/sass/machine.hpp from Python: ctypes mirrors of its thread, warp context
and environment, built from `evaluate.State`s and runtime contexts, and compiled fragment libraries
that execute single instructions on them."""
import ctypes
from sass.emulate import build
from sass.semantics import evaluate, primitive
from sass.semantics.runtime import Runtime, UnsupportedSemantics

SPACES = ("ram", "shared", "localmem", "cbank")
MESSAGE = 512
FILES = {"R": "r", "UR": "ur", "SR": "sr", "P": "p", "UP": "up"}


class Thread(ctypes.Structure):
    _fields_ = [("r", ctypes.c_uint32 * 256), ("ur", ctypes.c_uint32 * 256), ("sr", ctypes.c_uint32 * 256),
                ("p", ctypes.c_uint8 * 8), ("up", ctypes.c_uint8 * 8), ("pc", ctypes.c_uint64)]


class OperandGroups(ctypes.Structure):
    _fields_ = [("spans", ctypes.c_uint32 * 8), ("words", ((ctypes.c_uint32 * 8) * 32) * 8)]


class WarpContext(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in ("present", "lane", "active", "has_waiting", "waiting", "has_values", "has_predicates", "pad")] + \
               [("values", ctypes.c_uint64 * 32), ("predicates", ctypes.c_uint64 * 32),
                ("groups", ctypes.POINTER(OperandGroups)),
                ("has_bar_result", ctypes.c_uint32), ("bar_result", ctypes.c_uint32), ("async_state", ctypes.c_void_p),
                ("write_dependency", ctypes.c_uint32), ("async_ready", ctypes.c_uint32),
                ("has_bar_states", ctypes.c_uint32), ("bar_states", ctypes.c_uint32 * 16),
                ("cache", ctypes.c_void_p)]


class CacheRequest(ctypes.Structure):
    _fields_ = [("address", ctypes.c_uint64), ("operation", ctypes.c_uint32)]


class Cache:
    def __init__(self, library, state):
        self.lib, self.state = library, state

    def issue(self, operation, address):
        message = ctypes.create_string_buffer(MESSAGE)
        if self.lib.sass_cache_issue(self.state, operation, address, message, MESSAGE):
            raise UnsupportedSemantics(message.value.decode())

    def requests(self):
        count = self.lib.sass_cache_requests(self.state, None, 0)
        records = (CacheRequest * count)()
        self.lib.sass_cache_requests(self.state, records, count)
        return [primitive.cache_event(r.operation, r.address) for r in records]

    def generation(self): return self.lib.sass_cache_generation(self.state)


class AsyncCopy(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("address", "low", "high")] + [("size", ctypes.c_uint32)]


class Async:
    def __init__(self, library, state, lane, board):
        self.lib, self.state, self.lane, self.board = library, state, lane, board

    def issue(self, address, low, high, size, enabled):
        if enabled: self.lib.sass_async_issue(self.state, address, low, high, size)

    def commit(self):
        if self.board not in range(6): raise UnsupportedSemantics("async commit needs a write scoreboard")
        self.lib.sass_async_commit(self.state, self.board)

    def wait(self, board, count, mask):
        self.lib.sass_async_wait(self.state, board, count, mask)
        self.complete()

    def complete(self):
        count = self.lib.sass_async_ready(self.state, None, 0)
        copies = (AsyncCopy * count)()
        self.lib.sass_async_ready(self.state, copies, count)
        for copy in copies:
            value = copy.low | copy.high << 64
            self.lane.store("shared", copy.address, value.to_bytes(16, "little")[:copy.size])


class Range(ctypes.Structure):
    _fields_ = [("start", ctypes.c_uint64), ("last", ctypes.c_uint64), ("space", ctypes.c_uint32)]


class Environment(ctypes.Structure):
    _fields_ = [("sync", ctypes.c_uint32), ("has_ranges", ctypes.c_uint32), ("nranges", ctypes.c_uint32), ("nbmov", ctypes.c_uint32),
                ("ranges", ctypes.POINTER(Range)), ("ranges_error", ctypes.c_char_p),
                ("bmov_keys", ctypes.c_uint64 * 16), ("bmov_values", ctypes.c_uint64 * 16)]


def thread(state):
    t = Thread()
    for (file, index), value in state.regs.items(): getattr(t, FILES[file])[index] = value
    t.pc = state.pc or 0
    return t


def registers(t):
    """The thread's nonzero registers as `evaluate.State.regs`."""
    return {(file, i): v for file, field in FILES.items() for i, v in enumerate(getattr(t, field)) if v}


def warp(context):
    w, found = WarpContext(), context.get("warp")
    if cache := context.get("cache"): w.cache = cache.state
    if "barrier_result" in context: w.has_bar_result, w.bar_result = 1, int(context["barrier_result"])
    if (states := context.get("barrier_states")) is not None:
        if len(states) != 16: raise UnsupportedSemantics("sixteen barrier states required")
        w.has_bar_states, w.bar_states[:] = 1, [int(value) & 0xffffffff for value in states]
    if not found: return w
    w.present, w.lane, w.active = 1, int(found.get("lane", 0)), int(found.get("active", 0xffffffff))
    if found.get("waiting") is not None: w.has_waiting, w.waiting = 1, int(found["waiting"]) & 0xffffffff
    for key, flag in (("values", "has_values"), ("predicates", "has_predicates")):
        if (values := found.get(key)) is not None and len(values) == 32:
            setattr(w, flag, 1); getattr(w, key)[:] = [int(v) & (1 << 64) - 1 for v in values]
    if groups := found.get("operand_groups", {}):
        w._groups = OperandGroups()
        w.groups = ctypes.pointer(w._groups)
    for name, lanes in groups.items():
        if name not in primitive.OPERAND_GROUPS or len(lanes) != 32: raise ValueError("invalid warp operand group")
        span = len(lanes[0])
        if not 1 <= span <= 8 or any(len(row) != span for row in lanes): raise ValueError("invalid warp operand group span")
        group = primitive.OPERAND_GROUPS.index(name)
        w._groups.spans[group] = span
        for lane, words in enumerate(lanes):
            for word, value in enumerate(words): w._groups.words[group][lane][word] = int(value) & 0xffffffff
    return w


def environment(context):
    """The launch environment of a runtime context; the returned object keeps its arrays alive."""
    e, sync = Environment(), context.get("synchronization", {})
    e.sync = sum(1 << i for i, op in enumerate(primitive.SYNCHRONIZATION) if sync.get(op))
    if isinstance(bmov := sync.get("bmov"), dict):
        pairs = [(int(k), int(v)) for k, v in bmov.items()][:16]
        e.nbmov = len(pairs)
        for i, (k, v) in enumerate(pairs): e.bmov_keys[i], e.bmov_values[i] = k, v & (1 << 64) - 1
    if "address_spaces" in context:
        try:
            spans = [Range(start, end - 1, primitive_space(space)) for space, start, end in Runtime(0, context).address_ranges() if start < end]
            e.has_ranges, e.nranges, e._ranges = 1, len(spans), (Range * max(len(spans), 1))(*spans)
            e.ranges = ctypes.cast(e._ranges, ctypes.POINTER(Range))
        except UnsupportedSemantics as error: e._error = str(error).encode(); e.ranges_error = e._error
    return e


def primitive_space(space): return {"global": 1, "shared": 2, "local": 4, "shared_cluster": 8}[space]


class Library:
    """A compiled generated library and its memory entry points."""

    def __init__(self, source, stem):
        self.lib = lib = build.library(source, stem)
        lib.sass_memory_new.restype, lib.sass_memory_new.argtypes = ctypes.c_void_p, [ctypes.c_void_p]
        lib.sass_memory_free.argtypes = [ctypes.c_void_p]
        lib.sass_memory_write.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_size_t]
        lib.sass_memory_read.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_size_t]
        lib.sass_cache_new.restype = ctypes.c_void_p
        lib.sass_cache_free.argtypes = [ctypes.c_void_p]
        lib.sass_cache_generation.argtypes = [ctypes.c_void_p]
        lib.sass_cache_generation.restype = ctypes.c_uint64
        lib.sass_cache_issue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_size_t]
        lib.sass_cache_requests.argtypes = [ctypes.c_void_p, ctypes.POINTER(CacheRequest), ctypes.c_size_t]
        lib.sass_cache_requests.restype = ctypes.c_size_t
        lib.sass_async_issue.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32]
        lib.sass_async_commit.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.sass_async_wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
        lib.sass_async_ready.argtypes = [ctypes.c_void_p, ctypes.POINTER(AsyncCopy), ctypes.c_size_t]
        lib.sass_async_ready.restype = ctypes.c_size_t

    def memory(self, regions=(), base=None):
        """A new memory (freed with `free`) holding [(space, address, bytes)] over `base`."""
        m = self.lib.sass_memory_new(base)
        for space, address, data in regions: self.lib.sass_memory_write(m, SPACES.index(space), address, data, len(data))
        return m

    def read(self, m, space, address, size):
        out = ctypes.create_string_buffer(size)
        self.lib.sass_memory_read(m, SPACES.index(space), address, out, size)
        return out.raw

    def free(self, m): self.lib.sass_memory_free(m)


def runs(memory):
    """A State's memory as [(space, address, bytes)] of contiguous runs."""
    out = []
    for space, cells in memory.items():
        for address in sorted(cells):
            if out and out[-1][0] == space and out[-1][1] + len(out[-1][2]) == address: out[-1][2].append(cells[address])
            else: out.append((space, address, bytearray([cells[address]])))
    return [(s, a, bytes(d)) for s, a, d in out]


class Fragments(Library):
    """Independent single instructions, [(arch, word, address)] or lifted `Semantics`
    (`semantics=True`), each executed by `execute`."""

    def __init__(self, instructions, fp="exact", semantics=False, definition_calls=True):
        from sass.emulate import codegen
        source = (codegen.semantic_fragments(instructions, definition_calls) if semantics else
                  codegen.fragments(instructions, fp, definition_calls))
        super().__init__(source, "fragments")
        self.lib.sass_fragment.argtypes = [ctypes.c_int, ctypes.POINTER(Thread), ctypes.c_void_p, ctypes.POINTER(WarpContext),
                                           ctypes.POINTER(Environment), ctypes.c_char_p, ctypes.c_size_t]

    def execute(self, index, state, context, observe=()):
        """`evaluate.execute`'s result for instruction `index` on `state`, its memory holding the
        `observe` ranges [(space, address, size)]; raises UnsupportedSemantics with the runtime's message."""
        t, w, e = thread(state), warp(context), environment(context)
        m, message = self.memory(runs(state.memory)), ctypes.create_string_buffer(MESSAGE)
        try:
            failed = self.lib.sass_fragment(index, ctypes.byref(t), m, ctypes.byref(w), ctypes.byref(e), message, MESSAGE)
            if failed: raise UnsupportedSemantics(message.value.decode())
            out = evaluate.State(registers(t), {space: {} for space in SPACES}, t.pc)
            for space, address, size in observe:
                out.memory.setdefault(space, {}).update(zip(range(address, address + size), self.read(m, space, address, size)))
            return out
        finally: self.free(m)
