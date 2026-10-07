"""Evaluate md expressions (operand sizes in PREDICATES, CONDITIONS) against decoded operand values."""
import math, re
from functools import cache

ENUM_REF = re.compile(r'`(\w+)@("[^"]*"|[\w.]+)')
IDENT = re.compile(r"(?<![\w.])([A-Za-z_][\w.]*)(?![\w(])")

def ternaries(s):
    """Rewrite C `c ? a : b` (right-associative, lowest precedence) into Python, innermost groups first."""
    out, i = [], 0
    while i < len(s):  # recurse into parenthesised groups
        if s[i] == "(":
            depth, j = 1, i + 1
            while depth:
                depth += {"(": 1, ")": -1}.get(s[j], 0); j += 1
            out.append("(" + ternaries(s[i + 1:j - 1]) + ")"); i = j
        else:
            out.append(s[i]); i += 1
    s = "".join(out)
    depth, q = 0, None
    for i, ch in enumerate(s):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "?" and depth == 0: q = i; break
    if q is None: return s
    depth, nested = 0, 0
    for j in range(q + 1, len(s)):
        depth += {"(": 1, ")": -1}.get(s[j], 0)
        if depth == 0 and s[j] == "?": nested += 1
        elif depth == 0 and s[j] == ":":
            if nested: nested -= 1
            else: return f"(({ternaries(s[q + 1:j])}) if ({s[:q]}) else ({ternaries(s[j + 1:])}))"
    raise ValueError(f"unbalanced ?: in {s!r}")

@cache
def compile_expr(arch_name, expr):
    """md C-like expression -> (python source with enum refs resolved, free operand names)."""
    from sass import ir
    arch = ir.load(arch_name)
    py = ENUM_REF.sub(lambda m: str(arch.enums[m[1]][m[2].strip('"')]), expr)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    py = ternaries(py)
    names = {n for n in IDENT.findall(py) if n not in ("and", "or", "not", "if", "else", "ceil")}
    py = IDENT.sub(lambda m: m[1].replace(".", "__") if m[1] in names else m[1], py)
    return compile(py, expr, "eval"), frozenset(names)

def free_vars(arch, expr):
    return compile_expr(arch.name, expr)[1]

def evaluate(expr, arch, values):
    """Value of expr; `values` maps operand names to decoded integers (missing ones are 0)."""
    code, names = compile_expr(arch.name, expr)
    return eval(code, {"ceil": math.ceil, "__builtins__": {}}, {n.replace(".", "__"): values.get(n) or 0 for n in names})
