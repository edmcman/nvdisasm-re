from dataclasses import dataclass

from infer_types import Value, phi_aliases


class PrecisionLimit(Exception):
    pass


@dataclass(frozen=True)
class Atom:
    operation: str
    width: int
    arguments: tuple


@dataclass(frozen=True)
class Polynomial:
    width: int
    terms: tuple

    @staticmethod
    def make(width, terms):
        coefficients = {}
        for factors, coefficient in terms:
            factors = tuple(sorted(factors, key=repr))
            coefficients[factors] = (coefficients.get(factors, 0) + coefficient) % (1 << width)
        return Polynomial(width, tuple(sorted(((m, c) for m, c in coefficients.items() if c), key=repr)))

    @staticmethod
    def constant(width, value): return Polynomial.make(width, [((), value)])

    @staticmethod
    def atom(atom): return Polynomial.make(atom.width, [((atom,), 1)])

    def __add__(self, other):
        if self.width != other.width: raise PrecisionLimit("mixed-width addition")
        return Polynomial.make(self.width, self.terms + other.terms)

    def scale(self, coefficient): return Polynomial.make(self.width, [(m, c * coefficient) for m, c in self.terms])
    def __sub__(self, other): return self + other.scale(-1)

    def __mul__(self, other):
        if self.width != other.width: raise PrecisionLimit("mixed-width multiplication")
        if len(self.terms) * len(other.terms) > 256: raise PrecisionLimit("polynomial expansion exceeds 256 products")
        return Polynomial.make(self.width, [(a + b, x * y) for a, x in self.terms for b, y in other.terms])

    def constant_value(self):
        return 0 if not self.terms else self.terms[0][1] if len(self.terms) == 1 and not self.terms[0][0] else None

    def single_atom(self):
        return self.terms[0][0][0] if len(self.terms) == 1 and len(self.terms[0][0]) == 1 and self.terms[0][1] == 1 else None


class Normal:
    def __init__(self, graph):
        self.graph, self.aliases, self.memo = graph, phi_aliases(graph.values), {}
        self.projections = {}

    def atom(self, operation, width, *arguments): return Polynomial.atom(Atom(operation, width, arguments))

    def __call__(self, v):
        if v in self.memo: return self.memo[v]
        if v in self.aliases: return self(self.aliases[v])
        args = v.args
        match v.op:
            case "const": result = Polynomial.constant(v.width, args[0])
            case "input": result = self.atom("input", v.width, args[0])
            case "phi": result = self.atom("phi", v.width, v.index)
            case "add": result = self(args[0]) + self(args[1])
            case "sub": result = self(args[0]) - self(args[1])
            case "neg": result = self(args[0]).scale(-1)
            case "not" if v.width > 1: result = Polynomial.constant(v.width, -1) - self(args[0])
            case "mul": result = self(args[0]) * self(args[1])
            case "and": result = self.atom("and", v.width, *(self(a) for a in args))
            case "shl" if args[1].op == "const":
                result = self(args[0]).scale(1 << args[1].args[0]) if args[1].args[0] < v.width else Polynomial.constant(v.width, 0)
            case "extract": result = self.slice(args[0], *args[1:])
            case "concat": result = self.atom("concat", v.width, *(self(a) for a in args))
            case "zext" | "sext": result = self.extend(self(args[0]), v.width, v.op)
            case _:
                result = self.atom("opaque", v.width, v.index)
        self.memo[v] = result
        return result

    def slice(self, value, hi, lo):
        key = value, hi, lo
        if key in self.projections: return self.projections[key]
        pending, visited, leaves = [key], set(), set()
        while pending:
            current = pending.pop()
            if current in visited: continue
            visited.add(current)
            v, top, bottom = current
            match v.op:
                case "phi": pending.extend((a, top, bottom) for a in v.args)
                case "extract": pending.append((v.args[0], top + v.args[2], bottom + v.args[2]))
                case "concat":
                    offset, part = 0, None
                    for a in reversed(v.args):
                        if offset <= bottom <= top < offset + a.width: part = a, top - offset, bottom - offset
                        offset += a.width
                    if part is None: leaves.add(current)
                    else: pending.append(part)
                case _: leaves.add(current)
        if len(leaves) == 1:
            source, top, bottom = next(iter(leaves))
            result = self.extract(self(source), top, bottom)
        else:
            result = self.extract(self(value), hi, lo)
        self.projections[key] = result
        return result

    def extract(self, source, hi, lo):
        width = hi - lo + 1
        if width == source.width and lo == 0: return source
        constant = source.constant_value()
        if constant is not None: return Polynomial.constant(width, constant >> lo)
        atom = source.single_atom()
        if atom is not None and atom.operation == "extract":
            parent, _, offset = atom.arguments
            return self.extract(parent, hi + offset, lo + offset)
        if atom is not None and atom.operation in ("zext", "sext") and hi < atom.arguments[0].width:
            return self.extract(atom.arguments[0], hi, lo)
        if lo == 0 and atom is None:
            result = Polynomial.constant(width, 0)
            for factors, coefficient in source.terms:
                term = Polynomial.constant(width, coefficient)
                for factor in factors: term = term * self.extract(Polynomial.atom(factor), hi, 0)
                result = result + term
            return result
        return self.atom("extract", width, source, hi, lo)

    def extend(self, source, width, operation):
        if source.width == width: return source
        constant = source.constant_value()
        if constant is not None:
            if operation == "sext" and constant >= 1 << (source.width - 1): constant -= 1 << source.width
            return Polynomial.constant(width, constant)
        return self.atom(operation, width, source)


def signed(value, width): return value if value < 1 << (width - 1) else value - (1 << width)


def render(term):
    match term:
        case Atom("pure", _, (index, *_)): return f"%{index}"
        case Atom("iteration", _, (header,)): return f"iteration({header:#x})"
        case Atom("shr", _, (source, shift)): return f"({render(source)} >> {render(shift)})"
        case Atom("input", width, (name,)): return f"{name}:{width}"
        case Atom("coordinate", width, (name,)): return f"{name}:{width}"
        case Atom("word", _, (source,)): return f"int32({render(source)})"
        case Atom("phi", _, (index,)) | Atom("opaque", _, (index,)): return f"%{index}"
        case Atom("extract", _, (source, hi, lo)): return f"bits({render(source)}, {hi}:{lo})"
        case Atom("concat", _, parts): return "concat(" + ", ".join(map(render, parts)) + ")"
        case Atom("and", _, (a, b)): return f"({render(a)} & {render(b)})"
        case Atom(op, width, (source,)): return f"{op}{width}({render(source)})"
        case Polynomial(width, terms):
            pieces = []
            for factors, coefficient in terms:
                coefficient = signed(coefficient, width)
                product = " * ".join(render(a) for a in factors)
                pieces.append(str(coefficient) if not factors else product if coefficient == 1 else f"{coefficient} * {product}")
            return " + ".join(pieces) if pieces else "0"
        case _: raise TypeError(term)


def invariant(term):
    match term:
        case Atom("input"): return True
        case Atom("pure"): return True
        case Atom("extract" | "zext" | "sext" | "concat" | "and" | "shr"): return all(invariant(a) for a in term.arguments if isinstance(a, (Atom, Polynomial)))
        case Polynomial(): return all(invariant(a) for factors, _ in term.terms for a in factors)
        case _: return False


def execution_dependent(term):
    match term:
        case Atom("input", _, (name,)): return name.startswith("SR")
        case Atom("phi" | "opaque" | "iteration"): return True
        case Atom(): return any(execution_dependent(a) for a in term.arguments)
        case Polynomial(): return any(execution_dependent(a) for factors, _ in term.terms for a in factors)
        case _: return False


def dominators(blocks):
    if not blocks: return {}
    entry = min(blocks)
    result = {b: {b} if b == entry else set(blocks) for b in blocks}
    changed = True
    while changed:
        changed = False
        for b, block in blocks.items():
            if b == entry: continue
            parents = block["predecessors"]
            current = {b} | (set.intersection(*(result[p] for p in parents)) if parents else set())
            if current != result[b]: result[b], changed = current, True
    return result
