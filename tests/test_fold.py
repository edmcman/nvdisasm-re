"""Layer 0: decode-time folding (sass.sleigh.lower.Fold). SLEIGH constructor actions have only 64-bit
signed arithmetic and bit operations, so comparisons and ite are encoded arithmetically; the text
Fold writes, evaluated with those semantics, must equal sem_eval of the expression."""
import ast, random
import pytest
from sass.semantics import bitvec as sem
from sass.semantics.evaluate import evaluate
from sass.sleigh.lower import Fold

MASK64 = (1 << 64) - 1


def signed(v): v &= MASK64; return v - (1 << 64) if v >> 63 else v


class Action(ast.NodeTransformer):
    """Evaluate action text as SLEIGH does: every operation wraps to a signed 64-bit integer."""
    def __init__(self, fields): self.fields = fields

    def eval(self, node):
        match node:
            case ast.Expression(body=b): return self.eval(b)
            case ast.Constant(value=v): return signed(v)
            case ast.Name(id=n): return signed(self.fields[n])
            case ast.UnaryOp(op=ast.Invert(), operand=x): return signed(~self.eval(x))
            case ast.UnaryOp(op=ast.USub(), operand=x): return signed(-self.eval(x))
            case ast.BinOp(left=l, op=op, right=r):
                x, y = self.eval(l), self.eval(r)
                match op:
                    case ast.Add(): return signed(x + y)
                    case ast.Sub(): return signed(x - y)
                    case ast.Mult(): return signed(x * y)
                    case ast.BitAnd(): return signed(x & y)
                    case ast.BitOr(): return signed(x | y)
                    case ast.BitXor(): return signed(x ^ y)
                    case ast.LShift(): return signed(x << y)
                    case ast.RShift(): return x >> y   # arithmetic, like SLEIGH's signed shift
        raise ValueError(ast.dump(node))


class Fields:
    """A stand-in builder: every parameter is a plain field named after it."""
    class G:
        def immediate_text(self, k, name): return name, []
    def __init__(self): self.g, self.k = Fields.G(), None


def expressions(rng, depth, w):
    """A random expression of width w over parameters p0..p2 (each w bits)."""
    if depth == 0 or rng.random() < 0.2:
        return sem.param(f"p{rng.randrange(3)}", w) if rng.random() < 0.7 else sem.const(rng.getrandbits(w), w)
    sub = lambda: expressions(rng, depth - 1, w)
    match rng.choice(["and", "or", "xor", "not", "neg", "add", "sub", "mul", "shl", "shr", "ite", "cmp", "extract", "concat"]):
        case "and": return sub() & sub()
        case "or": return sub() | sub()
        case "xor": return sub() ^ sub()
        case "not": return ~sub()
        case "neg": return -sub()
        case "add": return sub() + sub()
        case "sub": return sub() - sub()
        case "mul": return sub() * sub()
        case "shl": return sub() << rng.randrange(w)
        case "shr": return sub() >> rng.randrange(w)
        case "ite":
            x, y = sub(), sub()
            return sem.ite(rng.choice([lambda: x == y, lambda: x != y, lambda: x < y, lambda: x <= y])(), sub(), sub())
        case "extract" if w > 1:
            lo = rng.randrange(w); x = expressions(rng, depth - 1, 64)
            return sem.zext(sem.extract(x, lo + w - 1, lo), w) if lo + w <= 64 else sub()
        case "concat" if w % 2 == 0:
            return sem.concat(expressions(rng, depth - 1, w // 2), expressions(rng, depth - 1, w // 2))
    return sub()


@pytest.mark.parametrize("w", [1, 8, 16, 32, 48, 63, 64])
def test_fold_matches_evaluation(w):
    rng = random.Random(f"fold-{w}")
    checked = 0
    for _ in range(300):
        e = expressions(rng, 4, w)
        if not isinstance(e, sem.Expr) or e.op in ("const", "param"): continue
        try: text = Fold(Fields()).text(e)
        except Fold.No: continue
        tree = ast.parse(text, mode="eval")
        for _ in range(8):
            values = {f"p{j}": rng.choice([0, 1, (1 << w) - 1, 1 << (w - 1), rng.getrandbits(w)]) for j in range(3)}
            want = evaluate(e, None, values)
            assert Action(values).eval(tree) & ((1 << w) - 1) == want, (e, text, values)
        checked += 1
    assert checked > 50
