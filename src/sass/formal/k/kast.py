"""Linear encoding of the exporter's ground term syntax to KAST v4.

This bypasses K's generalized text parser for large generated expression DAGs.
It is not a parser for arbitrary K definitions or symbolic claims.
"""
import re
from sass.formal import k

CONSTRUCTORS = {
    "node": ("Expr", ("Int", "String", "Int", "List")),
    "instruction": ("Instruction", ("Int", "OptionalExpr", "List", "List", "List", "OptionalFlow")),
    "unsupported": ("Instruction", ("Int", "OptionalExpr", "String")),
    "program": ("Program", ("Map", "Int")),
    "run": ("Input", ("Program", "Map", "Map", "Map", "Map", "Map", "Int")),
    "location": ("KItem", ("String", "Int")),
    "byte": ("KItem", ("String", "Int")),
    "write": ("KItem", ("String", "Int", "Int", "Expr")),
    "store": ("KItem", ("String", "Expr", "Expr", "OptionalExpr")),
    "effect": ("KItem", ("Int", "String", "List")),
    "request": ("KItem", ("Int", "Int", "Int")),
    "response": ("KItem", ("String", "List", "Int", "Int", "List", "Map", "Map")),
    "flow": ("OptionalFlow", ("String", "Expr", "OptionalExpr")),
}
LITERALS = {"absent": "absent_SASS_OptionalExpr", "noFlow": "noFlow_SASS_OptionalFlow",
            ".List": ".List", ".Map": ".Map"}
TOKEN = re.compile(r'\s*("(?:\\.|[^"\\])*"|-?[0-9]+|[A-Za-z_.][A-Za-z_0-9.]*|\|->|[(),])')


def apply(label, args):
    return dict(node="KApply", label=dict(node="KLabel", name=label, params=[]),
                arity=len(args), args=args)


def token(sort, value):
    return dict(node="KToken", sort=dict(node="KSort", name=sort, params=[]), token=value)


def joined(label, values):
    """Balanced trees avoid quadratic parsing and JSON recursion on long lists/maps."""
    if len(values) == 1: return values[0]
    middle = len(values) // 2
    return apply(label, [joined(label, values[:middle]), joined(label, values[middle:])])


class Parser:
    def __init__(self, term):
        self.tokens, position = [], 0
        while position < len(term):
            match = TOKEN.match(term, position)
            if not match:
                if not term[position:].strip(): break
                raise k.ExportError(f"not generated ground-term syntax at offset {position}; use structured=False")
            self.tokens.append(match[1])
            position = match.end()
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self, expected=None):
        value = self.peek()
        if value is None or expected is not None and value != expected:
            raise k.ExportError(f"expected {expected or 'a term'}, got {value}")
        self.index += 1
        return value

    def sequence(self):
        values = []
        while self.peek() not in (None, ",", ")"):
            value = self.atom()
            if self.peek() == "|->":
                self.take()
                value = apply("_|->_", [value, self.atom()])
            values.append(value)
        if not values: raise k.ExportError("empty ground term")
        if len(values) == 1: return values[0]
        labels = {v.get("label", {}).get("name") for v in values}
        if labels <= {".List", "ListItem", "_List_"}: return joined("_List_", values)
        if labels <= {".Map", "_|->_", "_Map_"}: return joined("_Map_", values)
        raise k.ExportError("invalid collection in ground term")

    def atom(self):
        value = self.take()
        if value.startswith('"'): return token("String", value)
        if re.fullmatch(r"-?[0-9]+", value): return token("Int", value)
        if value == "(":
            result = self.sequence()
            self.take(")")
            return result
        if value in LITERALS: return apply(LITERALS[value], [])
        if value != "ListItem" and value not in CONSTRUCTORS:
            raise k.ExportError(f"unknown ground-term constructor {value!r}; use structured=False")
        self.take("(")
        args = [self.sequence()]
        while self.peek() == ",":
            self.take()
            args.append(self.sequence())
        self.take(")")
        if value == "ListItem":
            if len(args) != 1: raise k.ExportError("ListItem needs one argument")
            return apply("ListItem", args)
        result, sorts = CONSTRUCTORS[value]
        if len(args) != len(sorts): raise k.ExportError(f"{value} has the wrong arity")
        label = f"{value}({','.join('_' for _ in sorts)})_SASS_{result}_{'_'.join(sorts)}"
        return apply(label, args)


def encode(term):
    parser = Parser(term)
    result = parser.sequence()
    if parser.peek() is not None: raise k.ExportError("trailing tokens in ground term")
    return dict(format="KAST", version=4, term=result)
