from dataclasses import dataclass

from access_relations import Atom, Polynomial, signed


@dataclass(frozen=True)
class Interval:
    lower: int
    upper: int

    def __post_init__(self):
        if self.lower > self.upper: raise ValueError(f"empty interval [{self.lower}, {self.upper}]")

    def __add__(self, other): return Interval(self.lower + other.lower, self.upper + other.upper)

    def __mul__(self, other):
        values = [a * b for a in (self.lower, self.upper) for b in (other.lower, other.upper)]
        return Interval(min(values), max(values))

    def intersect(self, other): return Interval(max(self.lower, other.lower), min(self.upper, other.upper))
    def report(self): return [self.lower, self.upper]

    def modulo(self, width):
        modulus = 1 << width
        if self.lower // modulus != self.upper // modulus: return Interval(0, modulus - 1)
        return Interval(self.lower % modulus, self.upper % modulus)

    def signed(self, width):
        midpoint, modulus = 1 << (width - 1), 1 << width
        if self.upper < midpoint: return self
        if self.lower >= midpoint: return self + Interval(-modulus, -modulus)
        return Interval(-midpoint, midpoint - 1)


def input_view(term):
    match term:
        case Atom("input", width, (name,)): return f"{name}:{width}"
        case Atom("extract", width, (Polynomial(_, (((Atom("input", _, (name,)),), 1),)), _, lo)) if name.startswith("cb") and lo % 8 == 0:
            return f"cb{int(name[2:], 16) + lo // 8:x}:{width}"
        case _: return None


class Ranges:
    def __init__(self, inputs=None):
        self.inputs, self.memo = {}, {}
        for key, (lower, upper) in (inputs or {}).items():
            width = int(key.rsplit(":", 1)[1])
            if not 0 <= lower <= upper < 1 << width: raise ValueError(f"invalid unsigned input range {key}: {[lower, upper]}")
            self.inputs[key] = Interval(lower, upper)

    def word(self, term):
        if term in self.memo: return self.memo[term]
        full = Interval(0, (1 << term.width) - 1)
        match term:
            case Polynomial(width, terms):
                result = Interval(0, 0)
                for factors, coefficient in terms:
                    c = signed(coefficient, width)
                    product = Interval(c, c)
                    for factor in factors: product = product * self.word(factor)
                    result = result + product
                result = result.modulo(width)
            case Atom("input"): result = full
            case Atom("extract", width, (source, _, lo)):
                parent = self.word(source)
                result = Interval(parent.lower >> lo, parent.upper >> lo).modulo(width)
            case Atom("zext", _, (source,)): result = self.word(source)
            case Atom("sext", width, (source,)): result = self.word(source).signed(source.width).modulo(width)
            case Atom("concat", _, parts):
                result, offset = Interval(0, 0), 0
                for part in reversed(parts):
                    result = result + self.word(part) * Interval(1 << offset, 1 << offset)
                    offset += part.width
            case Atom("and", _, (a, b)):
                left, right = self.word(a), self.word(b)
                result = Interval(0, min(left.upper, right.upper))
            case _: result = full
        key = input_view(term)
        if key in self.inputs: result = result.intersect(self.inputs[key])
        self.memo[term] = result
        return result

    def integer(self, term, signed_interpretation):
        result = self.word(term)
        return result.signed(term.width) if signed_interpretation else result
