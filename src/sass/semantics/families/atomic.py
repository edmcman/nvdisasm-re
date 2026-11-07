from sass.semantics.bitvec import concat, const, extract, fnan, ite, load, op, slt
from sass.semantics.family import definition, family, unmodelled
from sass.semantics.families import floating, memory
from sass.semantics import ordered


@definition(lambda a: (f"atomic.add.{a['size'].lower()}", ["old", "value"]))
def float_add(size, old, value):
    """Atomic RN addition: F32 flushes denormals; F64 preserves source NaNs, preferring value."""
    def add(a, b):
        result = op("fadd", a, b, "RN", w=a.w)
        nan = {16: 0x7fff, 32: 0x7fffffff, 64: 0xfff8000000000000}[a.w]
        return ite(fnan(result), const(nan, a.w), result)
    match size:
        case "F32.FTZ.RN": return floating.flushed(add(floating.flushed(old), floating.flushed(value)))
        case "F64.RN": return ite(fnan(value), value, ite(fnan(old), old, add(old, value)))
        case "F16x2.RN":
            return concat(*(add(extract(old, n + 15, n), extract(value, n + 15, n)) for n in (16, 0)))
        case _: unmodelled(f"atomic type {size}")


@definition(lambda a: (f"atomic.{a['op'].lower()}.{'s' if a['signed'] else 'u'}{a['old'].w}",
                       ["old", "value", *(["replacement"] if a["op"] == "CAS" else [])]))
def update(op, old, value, replacement=None, signed=False):
    """The value committed by a PTX integer atomic; the returned value is the original memory."""
    match op:
        case "ADD": return old + value
        case "MIN": return ite(slt(old, value) if signed else old < value, old, value)
        case "MAX": return ite(slt(old, value) if signed else old < value, value, old)
        case "INC": return ite(old >= value, const(0, old.w), old + 1)
        case "DEC": return ite((old == 0) | (old > value), value, old - 1)
        case "AND": return old & value
        case "OR": return old | value
        case "XOR": return old ^ value
        case "EXCH": return value
        case "CAS": return ite(old == value, replacement, old)
        case _: unmodelled(f"atomic operation {op}")


@family("ATOMG", modifier_roles={"cop": "hint", "sem": "effect", "sco": "effect", "private": "effect"})
def atomg(i):
    size = str(i.select_fork("sz"))
    sizes = {"32": 32, "S32": 32, "64": 64, "S64": 64, "128": 128,
             "F32.FTZ.RN": 32, "F64.RN": 64, "F16x2.RN": 32}
    if size not in sizes: unmodelled(f"atomic type {size}")
    width = sizes[size]
    op = str(i.select_fork("cas" if i.has_operand("cas") else "op"))
    if op == "SAFEADD": unmodelled("SAFEADD overflow and predicate result")
    if i.select_fork("sem") == "MMIO": unmodelled("atomic MMIO environment")
    address = memory.address(i)
    body = i.body()
    i.emit_effects(body, [memory.ordering(i), *memory.descriptor(i)])
    inner = ordered.Builder(body.count)
    address = inner.capture(address)
    old = inner.read_memory("ram", address, width)
    value = i.value("Rb", modifiers=False)
    if value.w != width: unmodelled("atomic source span")
    replacement = i.value("Rc", modifiers=False) if i.has_operand("Rc") else None
    if replacement is not None and replacement.w != width: unmodelled("CAS replacement span")
    if size.startswith("F") and op != "ADD": unmodelled(f"atomic {op}.{size}")
    value = inner.capture(value)
    replacement = inner.capture(replacement) if replacement is not None else None
    result = float_add(size, old, value) if size.startswith("F") else update(op, old, value, replacement, size.startswith("S"))
    result = inner.move(result)
    i.assign_from_inputs(inner, Rd=old, Pu=const(0, 1))
    inner.emit(ordered.WriteMemory("ram", address, result))
    body.emit(ordered.Atomic(inner.block()))
    return body.block()


@family("MEMBAR", modifier_roles={"sem": "effect", "sco": "effect", "mmio": "effect"})
def membar(i):
    if not i.has_operand("sem"): unmodelled("async fence obligations")
    sem, scope = str(i.select_fork("sem")), str(i.select_fork("sco"))
    if sem not in ("SC", "ALL", "nomembar_sem"): unmodelled(f"fence semantics {sem}")
    if scope not in ("CTA", "SM", "GPU", "SYS"): unmodelled(f"fence scope {scope}")
    if i.has_operand("mmio") and i.select_fork("mmio") != "nomembar_mmio":
        unmodelled("MMIO/async fence")
    body = ordered.Builder()
    body.special(ordered.FENCE, const({"SC": 0, "ALL": 1, "nomembar_sem": 2}[sem], 32),
                 const({"CTA": 0, "SM": 1, "GPU": 2, "SYS": 3}[scope], 32))
    return body.block()
