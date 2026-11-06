from dataclasses import dataclass


@dataclass(frozen=True)
class Variable:
    index: int
    sort: str = "value"
    width: int | None = None


@dataclass(frozen=True)
class Type:
    tag: str
    args: tuple = ()
    sort: str = "value"
    width: int | None = None


NUMERIC = Type("numeric", sort="role")
ADDRESS = Type("address", sort="role")


def floating(width): return Type("float", width=width)
def integer(width): return Type("integer", width=width) if width != 1 else predicate()
def predicate(): return Type("predicate", width=1)
def pointer(space, width, element, shape): return Type("pointer", (space, element, shape), width=width)
def shape(*dimensions): return Type("shape", dimensions, sort="shape")
def dimension(value): return Type("dimension", (value,), sort="dimension")
def arrow(argument, result): return Type("arrow", (argument, result))


class InferenceError(Exception):
    pass


class Solver:
    def __init__(self):
        self.variables, self.bindings, self.reasons, self.constraints = [], {}, {}, []

    def fresh(self, sort="value", width=None):
        v = Variable(len(self.variables), sort, width)
        self.variables.append(v)
        return v

    def root(self, term):
        while isinstance(term, Variable) and term in self.bindings: term = self.bindings[term]
        return term

    def resolve(self, term):
        term = self.root(term)
        return Type(term.tag, tuple(self.resolve(a) for a in term.args), term.sort, term.width) if isinstance(term, Type) else term

    def occurs(self, variable, term):
        term = self.root(term)
        return variable == term or (isinstance(term, Type) and any(self.occurs(variable, a) for a in term.args))

    def constrain(self, left, right, origin):
        self.constraints.append((left, right, origin))

    def unify(self, left, right, origin):
        pending = [(left, right)]
        while pending:
            a, b = pending.pop()
            a, b = self.root(a), self.root(b)
            if a == b: continue
            if isinstance(b, Variable) and not isinstance(a, Variable): a, b = b, a
            if isinstance(a, Variable):
                if isinstance(b, Variable) and a.width is not None and b.width is None: a, b = b, a
                if not isinstance(b, (Variable, Type)) or a.sort != b.sort:
                    raise InferenceError(f"sort mismatch at {origin}: {a} = {b}")
                if a.width is not None and a.width != b.width:
                    raise InferenceError(f"width mismatch at {origin}: {a.width} != {b.width}")
                if self.occurs(a, b): raise InferenceError(f"occurs check at {origin}: {a} in {b}")
                self.bindings[a], self.reasons[a] = b, origin
            elif isinstance(a, Type) and isinstance(b, Type):
                if (a.tag, a.sort, a.width, len(a.args)) != (b.tag, b.sort, b.width, len(b.args)):
                    raise InferenceError(f"type mismatch at {origin}: {self.resolve(a)} != {self.resolve(b)}")
                pending.extend(zip(a.args, b.args))
            else:
                raise InferenceError(f"type parameter mismatch at {origin}: {a} != {b}")

    def solve(self):
        for a, b, origin in self.constraints: self.unify(a, b, origin)


@dataclass(frozen=True)
class Scheme:
    variables: frozenset
    body: object

    def instantiate(self, solver):
        substitution = {v: solver.fresh(v.sort, v.width) for v in self.variables}
        def walk(term):
            match term:
                case Variable(): return substitution.get(term, term)
                case Type(tag, args, sort, width): return Type(tag, tuple(map(walk, args)), sort, width)
                case _: return term
        return walk(self.body)


class Printer:
    def __init__(self, solver): self.solver, self.names = solver, {}

    def __call__(self, term):
        term = self.solver.resolve(term)
        match term:
            case Variable(_, sort, width):
                prefix = {"value": "t", "shape": "s", "dimension": "d", "role": "k"}[sort]
                name = self.names.setdefault(term, f"'{prefix}{len(self.names)}")
                return f"{name}:{width}" if width is not None else name
            case Type("float", _, _, width): return f"f{width}"
            case Type("integer", _, _, width): return f"i{width}"
            case Type("predicate"): return "bool"
            case Type("pointer", (space, element, layout), _, width):
                return f"ptr{width}<{space}, {self(element)}, {self(layout)}>"
            case Type("shape", dimensions): return "[" + ", ".join(map(self, dimensions)) + "]"
            case Type("dimension", (value,)): return str(value)
            case Type("arrow", (argument, result)): return f"({self(argument)} -> {self(result)})"
            case Type(tag, ()): return tag
            case _: return str(term)
