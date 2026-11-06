from dataclasses import replace

from sass.semantics import bitvec
from sass.semantics.family import Flow
from type_system import InferenceError


def expand(sems, limit=50000, depth=16):
    original = {s.addr: s for s in sems}
    if not any(s.flow and (s.flow.kind == "call" or (s.flow.kind == "return" and not bitvec.is_const(s.flow.target, 0))) for s in sems):
        return sems, {}, {}
    span = max(original) + 16
    contexts, pending, visited, output, checks, locations = {(): 0}, [(0, ())], set(), [], {}, {}
    def address(pc, stack):
        if stack not in contexts: contexts[stack] = len(contexts) * span
        return contexts[stack] + pc
    while pending:
        pc, stack = pending.pop()
        if (pc, stack) in visited: continue
        if pc not in original: raise InferenceError(f"control transfer outside text at {pc:#x}")
        if len(visited) >= limit or len(stack) > depth: raise InferenceError("call-context expansion limit exceeded")
        visited.add((pc, stack))
        s = original[pc]
        target, virtual, flow = None, address(pc, stack), s.flow
        successors = []
        if flow:
            match flow.kind:
                case "call":
                    if not bitvec.is_const(flow.target): raise InferenceError(f"indirect call at {pc:#x}")
                    next_stack = stack + (pc + 16,)
                    target = bitvec.value(flow.target), next_stack
                case "goto":
                    if not bitvec.is_const(flow.target): raise InferenceError(f"indirect jump at {pc:#x}")
                    target = bitvec.value(flow.target), stack
                case "return" if stack and not bitvec.is_const(flow.target, 0):
                    checks[virtual] = flow.target, stack[-1]
                    target = stack[-1], stack[:-1]
                case "return" if not stack and not bitvec.is_const(flow.target, 0):
                    raise InferenceError(f"return without a call context at {pc:#x}")
                case "return": pass
                case _: raise InferenceError(f"unsupported control flow {flow.kind} at {pc:#x}")
            if target is not None:
                successors.append(target)
                flow = Flow("goto", bitvec.const(address(*target), 64), flow.cond)
        if s.flow is None or s.guard is not None or s.flow.cond is not None: successors.append((pc + 16, stack))
        locations[virtual] = {"original_address": pc, "return_context": list(stack)}
        output.append(replace(s, addr=virtual, flow=flow))
        pending.extend(successors)
    return sorted(output, key=lambda s: s.addr), checks, locations


def constants(graph, roots):
    pending, needed = list(roots), set()
    while pending:
        value = pending.pop()
        if value in needed: continue
        needed.add(value)
        pending.extend(a for a in value.args if hasattr(a, "op"))
    result = {v: None for v in needed}
    def join(a, b):
        if a is None: return b
        if b is None: return a
        av, am = a
        bv, bm = b
        mask = am & bm & ~(av ^ bv)
        return av & mask, mask
    changed = True
    while changed:
        changed = False
        for value in sorted(needed, key=lambda v: v.index):
            args, mask = value.args, (1 << value.width) - 1
            incoming = [result[a] if hasattr(a, "op") else (a, -1) for a in args]
            match value.op:
                case "const": known = args[0], mask
                case "phi":
                    known = None
                    for term in incoming: known = join(known, term)
                case "ite":
                    condition, yes, no = incoming
                    known = None if condition is None else (yes if condition[0] else no) if condition[1] == 1 else join(yes, no)
                case "concat" if all(a is not None for a in incoming):
                    bits, defined = 0, 0
                    for argument, (part, part_mask) in zip(args, incoming):
                        bits, defined = (bits << argument.width) | part, (defined << argument.width) | part_mask
                    known = bits, defined
                case "extract" if incoming[0] is not None:
                    bits, defined = incoming[0]
                    known = (bits >> args[2]) & mask, (defined >> args[2]) & mask
                case "zext" if incoming[0] is not None:
                    bits, defined = incoming[0]
                    known = bits, defined | (mask ^ ((1 << args[0].width) - 1))
                case "concat" | "extract" | "zext": known = None
                case "add" | "sub" | "mul" | "and" | "or" | "xor" | "not" | "shl" | "shr" | "sext":
                    if any(a is None for a in incoming): known = None
                    elif all(not hasattr(a, "width") or k[1] == (1 << a.width) - 1 for a, k in zip(args, incoming)):
                        bits = bitvec.OPS[value.op]([a[0] for a in incoming], [a.width if hasattr(a, "width") else None for a in args], value.width)
                        known = bits & mask, mask
                    else: known = 0, 0
                case _: known = (0, 0)
            previous = result[value]
            merged = join(previous, known)
            if merged != previous: result[value], changed = merged, True
    return {v: frozenset([bits[0]]) if bits is not None and bits[1] == (1 << v.width) - 1 else None for v, bits in result.items()}


def verify_returns(graph):
    result = constants(graph, [target for target, _ in graph.return_checks.values()])
    verified = []
    for origin, (target, expected) in graph.return_checks.items():
        possible = result[target]
        if possible != frozenset([expected]): raise InferenceError(f"return target not proved at {origin}: expected {expected:#x}, got {possible}")
        verified.append({"origin": origin, "return_target": expected, "status": "proved"})
    graph.verified_returns = verified
