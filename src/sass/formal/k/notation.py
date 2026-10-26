"""A mathematical presentation of the concrete K syntax used by these modules.

This is deliberately a bounded parser, not a replacement for K's parser. The
precedences below follow builtin/domains.md in the pinned K 7.1.337 release.
Unknown syntax fails publication instead of being silently reformulated.
"""
from dataclasses import dataclass
import re


# K's Boolean connectives share one precedence; exponentiation is left-associative.
PRIORITY = {
    "~>": 5, "|->": 26,
    "==Bool": 12, "=/=Bool": 12,
    "andBool": 15, "orBool": 15, "xorBool": 15,
    "==Int": 30, "=/=Int": 30, "<Int": 30, "<=Int": 30,
    ">Int": 30, ">=Int": 30, "==String": 30, "=/=String": 30,
    "==K": 30, "=/=K": 30, "in_keys": 30,
    "|Int": 35, "xorInt": 40, "&Int": 45,
    "<<Int": 50, ">>Int": 50,
    "+Int": 60, "-Int": 60, "+String": 60,
    "*Int": 70, "/Int": 70, "modInt": 70, "^Int": 80,
}
SYMBOL = {
    "~>": r"\mathbin{\curvearrowright}", "|->": r"\mapsto",
    "andBool": r"\land", "orBool": r"\lor", "xorBool": r"\mathbin{\oplus_{\!B}}",
    "==Bool": r"\mathrel{=_{\!B}}", "=/=Bool": r"\mathrel{\ne_{\!B}}",
    "==Int": "=", "=/=Int": r"\ne", "<Int": "<", "<=Int": r"\le",
    ">Int": ">", ">=Int": r"\ge",
    "==String": r"\mathrel{=_{\!S}}", "=/=String": r"\mathrel{\ne_{\!S}}",
    "==K": r"\mathrel{=_{\!K}}", "=/=K": r"\mathrel{\ne_{\!K}}",
    "|Int": r"\mathbin{|}", "xorInt": r"\oplus", "&Int": r"\mathbin{\&}",
    "<<Int": r"\ll", ">>Int": r"\gg", "+Int": "+", "-Int": "-",
    "*Int": r"\cdot", "/Int": r"\mathbin{\div_{\!t}}",
    "modInt": r"\mathbin{\mathrm{mod}_{\!E}}",
    "+String": r"\mathbin{+\!\!+}_{S}",
}
# Conventional mathematical precedence, used to make the K grouping explicit.
MATH_PRIORITY = {**PRIORITY, "andBool": 18, "in_keys": 30}
CONSTANTS = frozenset("""
true false fpNaN absent noFlow next discard setTarget commit skip finishStep
""".split())
TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"|\.List|\.Map|\.K|'
    + "|".join(re.escape(op) + (r"\b" if op[-1].isalnum() else "")
               for op in sorted(PRIORITY, key=len, reverse=True))
    + r"|notBool\b|:>|<-|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[(){},\[\]:]"
)


def escape(text):
    """Escape text inside a TeX text/font command."""
    return "".join({
        "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}",
        "$": r"\$", "&": r"\&", "#": r"\#", "%": r"\%",
        "_": r"\_", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
        "<": r"\textless{}", ">": r"\textgreater{}",
    }.get(char, char) for char in text)


def tokens(text):
    result, end = [], 0
    for match in TOKEN.finditer(text):
        if text[end:match.start()].strip():
            raise ValueError(f"unsupported K notation: {text[end:match.start()]!r}")
        result.append(match[0])
        end = match.end()
    if text[end:].strip():
        raise ValueError(f"unsupported K notation: {text[end:]!r}")
    return result


@dataclass(frozen=True)
class Piece:
    tex: str
    width: float
    break_before: int = 0


def literal(text, *, break_before=False):
    return Piece(text, 1 if text.startswith("\\") else len(text), break_before)


def nested(pieces):
    """Prefer outer argument/operator boundaries to breaks inside a subterm."""
    return [Piece(piece.tex, piece.width,
                  piece.break_before + 100 if piece.break_before else 0)
            for piece in pieces]


@dataclass(frozen=True)
class Term:
    kind: str
    value: str
    children: tuple = ()

    def k(self):
        """Reconstruct the source tokens, independently of the mathematical view."""
        kids = [child.k() for child in self.children]
        if self.kind == "atom":
            return self.value
        if self.kind == "call":
            return self.value + "(" + ", ".join(kids) + ")"
        if self.kind == "group":
            return self.value + kids[0] + {"(": ")", "{": "}"}[self.value]
        if self.kind == "binary":
            return f"{kids[0]} {self.value} {kids[1]}"
        if self.kind == "concat":
            return " ".join(kids)
        if self.kind == "prefix":
            return self.value + " " + kids[0]
        if self.kind == "cast":
            return kids[0] + self.value + kids[1]
        if self.kind == "index":
            return kids[0] + "[" + kids[1] + "]"
        if self.kind == "update":
            return kids[0] + "[" + kids[1] + " <- " + kids[2] + "]"
        raise ValueError(self.kind)

    def pieces(self, parent=0, *, right=False):
        """Break opportunities respect syntax; strings and powers stay intact."""
        if self.kind == "atom":
            if self.value.startswith('"'):
                return [Piece(r"\text{\ttfamily " + escape(self.value) + "}",
                              (len(self.value) - 2) * .65 + 2)]
            if self.value in (".List", ".Map", ".K"):
                glyph = {".List": r"\epsilon", ".Map": r"\varnothing", ".K": r"\bullet"}[self.value]
                return [Piece(glyph + r"_{\mathrm{" + self.value[1:] + "}}", 2.5)]
            if self.value == "_":
                return [Piece(r"\_", 1)]
            if self.value[0].isupper():
                return [Piece(r"\mathit{" + escape(self.value) + "}", len(self.value) * .7)]
            if self.value.isdigit():
                return [literal(self.value)]
            return [Piece(r"\mathsf{" + escape(self.value) + "}", len(self.value) * .65)]
        if self.kind == "binary":
            left, rhs = self.children
            if self.value == "^Int":
                exponent = rhs.children[0] if rhs.kind == "group" and rhs.value == "(" else rhs
                tex = "{" + left.tex(81) + "}^{" + exponent.tex() + "}"
                if parent > 80:
                    tex = r"\bigl(" + tex + r"\bigr)"
                return [Piece(tex, left.width() + exponent.width() * .7)]
            priority = MATH_PRIORITY[self.value]
            parts = left.pieces(priority)
            if self.value == "in_keys":
                domain = rhs.children[0] if rhs.kind == "group" and rhs.value == "(" else rhs
                parts += [Piece(r"\in\operatorname{dom}\bigl(", 5, priority),
                          *domain.pieces(), literal(r"\bigr)")]
            else:
                parts += [Piece(SYMBOL[self.value], 2, priority),
                          *rhs.pieces(priority, right=True)]
            # K and conventional math can group differently; never silently regroup.
            if priority < parent or (right and priority == parent):
                return [literal(r"\bigl("), *parts, literal(r"\bigr)")]
            return parts
        if self.kind == "group":
            opening, closing = (r"\bigl(", r"\bigr)") if self.value == "(" else (r"\{", r"\}")
            return [Piece(opening, 1), *nested(self.children[0].pieces()), Piece(closing, 1)]
        if self.kind == "prefix":
            return [Piece(r"\neg", 1), *self.children[0].pieces(20)]
        if self.kind == "concat":
            left, rhs = self.children
            parts = [*left.pieces(24), Piece(r"\mathbin{\Vert}", 2, 24), *rhs.pieces(24)]
            if parent > 24:
                return [Piece(r"\bigl(", 1), *parts, Piece(r"\bigr)", 1)]
            return parts
        if self.kind == "call":
            if self.value == "boolBit":
                opening, closing, name_width = r"\mathbf{1}\bigl[", r"\bigr]", 2
            elif self.value == "ListItem":
                opening, closing, name_width = r"\bigl[", r"\bigr]", 1
            elif self.value == "absInt":
                opening, closing, name_width = r"\bigl|", r"\bigr|", 1
            else:
                name = {"minInt": "min", "maxInt": "max"}.get(self.value, self.value)
                opening = r"\operatorname{" + escape(name) + r"}\bigl("
                closing, name_width = r"\bigr)", len(name) * .65 + 1
            parts = [Piece(opening, name_width)]
            for index, child in enumerate(self.children):
                if index:
                    parts.append(Piece(r",\,", 1))
                child_parts = nested(child.pieces())
                if index:
                    first = child_parts[0]
                    child_parts[0] = Piece(first.tex, first.width, 1)
                parts.extend(child_parts)
            return [*parts, Piece(closing, 1)]
        if self.kind == "cast":
            term, sort = self.children
            symbol = ":" if self.value == ":" else r"\mathbin{\triangleright}"
            return [*term.pieces(), Piece(symbol + r"\mathrm{" + escape(sort.value) + "}",
                                         1 + len(sort.value) * .6)]
        if self.kind in ("index", "update"):
            parts = [*self.children[0].pieces(), Piece(r"\bigl[", 1), *self.children[1].pieces()]
            if self.kind == "update":
                parts += [Piece(r"\leftarrow", 2, 1), *self.children[2].pieces()]
            return [*parts, Piece(r"\bigr]", 1)]
        raise ValueError(self.kind)

    def tex(self, parent=0):
        return "".join(piece.tex + " " for piece in self.pieces(parent)).rstrip()

    def width(self):
        return sum(piece.width for piece in self.pieces())


class Parser:
    def __init__(self, text):
        self.tokens = tokens(text)
        self.index = 0

    def peek(self):
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def take(self, expected=None):
        value = self.peek()
        if not value or (expected is not None and value != expected):
            raise ValueError(f"expected {expected or 'term'}, got {value!r}")
        self.index += 1
        return value

    def expression(self, minimum=0):
        value = self.take()
        if value == "notBool":
            left = Term("prefix", value, (self.expression(20),))
        elif value in ("(", "{"):
            child = self.expression()
            self.take({"(": ")", "{": "}"}[value])
            left = Term("group", value, (child,))
        elif re.fullmatch(r'[A-Za-z_]\w*|[0-9]+|\.List|\.Map|\.K|"(?:\\.|[^"\\])*"', value):
            if self.peek() == "(":
                self.take()
                args = []
                if self.peek() != ")":
                    args.append(self.expression())
                    while self.peek() == ",":
                        self.take()
                        args.append(self.expression())
                self.take(")")
                left = Term("call", value, tuple(args))
            else:
                if value[0].islower() and value not in CONSTANTS:
                    raise ValueError(f"unsupported K symbol: {value!r}")
                left = Term("atom", value)
        else:
            raise ValueError(f"expected K term, got {value!r}")
        while True:
            op = self.peek()
            if op in (":", ":>") and minimum <= 100:
                self.take()
                sort = self.take()
                if not re.fullmatch(r"[A-Z]\w*", sort):
                    raise ValueError(f"expected sort, got {sort!r}")
                left = Term("cast", op, (left, Term("atom", sort)))
            elif op == "[" and minimum <= 100:
                self.take()
                key = self.expression()
                if self.peek() == "<-":
                    self.take()
                    rhs = self.expression()
                    left = Term("update", "", (left, key, rhs))
                else:
                    left = Term("index", "", (left, key))
                self.take("]")
            elif op in PRIORITY and PRIORITY[op] >= minimum:
                self.take()
                left = Term("binary", op, (left, self.expression(PRIORITY[op] + 1)))
            elif (minimum <= 24 and op and
                  op not in PRIORITY and op != "notBool" and
                  (op in ("(", "{", ".List", ".Map", ".K") or op.startswith('"')
                   or re.fullmatch(r"[A-Za-z_]\w*|[0-9]+", op))):
                left = Term("concat", "", (left, self.expression(25)))
            else:
                break
        return left


def parse(text):
    parser = Parser(text)
    term = parser.expression()
    if parser.peek():
        raise ValueError(f"unexpected token {parser.peek()!r}")
    return term


def rows(term, width):
    """Wrap mathematical pieces at syntax boundaries, without font scaling."""
    pieces, result, start = term.pieces(), [], 0
    while start < len(pieces):
        size, split, candidates = 0, None, []
        for index in range(start, len(pieces)):
            piece = pieces[index]
            if index > start and piece.break_before:
                split = index
                if size <= width:
                    candidates.append((piece.break_before, index, size))
            size += piece.width
            if size > width and split is not None:
                balanced = [candidate for candidate in candidates if candidate[2] >= width * .45]
                if balanced:
                    split = min(balanced, key=lambda candidate: (candidate[0], -candidate[1]))[1]
                break
        else:
            split = len(pieces)
        if split is None:
            split = len(pieces)
        result.append(" ".join(piece.tex for piece in pieces[start:split]))
        start = split
    return result
