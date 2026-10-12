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
def python_source(arch_name, expr):
    """md C-like expression -> (python source with enum refs resolved, free operand names)."""
    from sass import ir
    arch = ir.load(arch_name)
    py = ENUM_REF.sub(lambda m: str(arch.enums[m[1]][m[2].strip('"')]), expr)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    py = ternaries(py)
    names = {n for n in IDENT.findall(py) if n not in ("and", "or", "not", "if", "else", "ceil")}
    py = IDENT.sub(lambda m: m[1].replace(".", "__") if m[1] in names else m[1], py)
    return py.strip(), frozenset(names)

@cache
def compile_expr(arch_name, expr):
    py, names = python_source(arch_name, expr)
    return compile(py, expr, "eval"), names

DEFINED = re.compile(r"DEFINED\s+(\w+)\s*\(")
ATTR = re.compile(r"(?<![\w`])([A-Za-z_]\w*)@(\w+)")
CONSTANT = re.compile(r"[%$]([A-Za-z_]\w*)")
TABLE_CALL = re.compile(r"(?<!\w)(TABLES_\w+)\s*\(")

def implication(s):
    """`a -> b` (lowest precedence, right-associative) as `(not (a)) or (b)`, in every group."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "(":
            depth, j = 1, i + 1
            while depth:
                depth += {"(": 1, ")": -1}.get(s[j], 0); j += 1
            out.append("(" + implication(s[i + 1:j - 1]) + ")"); i = j
        else:
            out.append(s[i]); i += 1
    s, depth = "".join(out), 0
    for i in range(len(s) - 1):
        depth += {"(": 1, ")": -1}.get(s[i], 0)
        if depth == 0 and s[i:i + 2] == "->":
            return f"(not ({s[:i]})) or ({implication(s[i + 2:])})"
    return s

@cache
def condition_source(arch_name, expr):
    """md CONDITIONS expression -> (python source, operand names, tables used by DEFINED).

    Operand attributes `Ra@negate` become names `Ra__AT__negate`; `%PARAM`/`$CONST` are inlined;
    `DEFINED T(a, b)` asks whether the operand values form a row key of table T."""
    from sass import ir
    arch = ir.load(arch_name)
    s = ENUM_REF.sub(lambda m: str(arch.enums[m[1]][m[2].strip('"')]), expr)
    s = CONSTANT.sub(lambda m: str(arch.constants[m[1]]), s)
    tables = tuple(dict.fromkeys(DEFINED.findall(s) + TABLE_CALL.findall(s)))
    s = DEFINED.sub(lambda m: f"__defined_{m[1]}(", s)
    s = TABLE_CALL.sub(lambda m: f"__table_{m[1]}(", s)
    s = ATTR.sub(lambda m: f"{m[1]}__AT__{m[2]}", s)
    py, names = python_source(arch_name, implication(s))
    return py, names, tables

@cache
def compile_condition(arch_name, expr):
    py, names, tables = condition_source(arch_name, expr)
    return compile(py, expr, "eval"), names, tables

def env_key(name):
    """Python name in a condition -> pydecode env key."""
    return tuple(name.split("__AT__")) if "__AT__" in name else name

def condition_globals(arch_name, tables):
    return {"ceil": math.ceil, "__builtins__": {}, **{f"__defined_{t}": defined(arch_name, t) for t in tables},
            **{f"__table_{t}": lookup(arch_name, t) for t in tables}}

@cache
def table_rows(arch_name, table):
    """[(key tuple with None wildcards, output)] of an md table, resolved."""
    from sass import ir, pydecode
    arch = ir.load(arch_name)
    return [(tuple(None if t == "-" else pydecode.resolve(arch, t) for t in lhs), pydecode.resolve(arch, out))
            for lhs, out in arch.tables[table]]

@cache
def row(arch_name, table, vs):
    return next((out for key, out in table_rows(arch_name, table) if all(k is None or k == v for k, v in zip(key, vs))), None)

def defined(arch_name, table):
    return lambda *vs: row(arch_name, table, vs) is not None

def lookup(arch_name, table):
    """`T(a, b)` in a condition: the output of the first row matching the arguments (None if none)."""
    return lambda *vs: row(arch_name, table, vs)

def holds(arch, expr, env):
    """Whether a CONDITIONS expression holds for decoded values `env` (pydecode keys: name or (name, attr))."""
    code, names, tables = compile_condition(arch.name, expr)
    values = {n.replace(".", "__"): env.get(env_key(n)) or 0 for n in names}
    return bool(eval(code, condition_globals(arch.name, tables), values))

def free_vars(arch, expr):
    return compile_expr(arch.name, expr)[1]

def evaluate(expr, arch, values):
    """Value of expr; `values` maps operand names to decoded integers (missing ones are 0)."""
    code, names = compile_expr(arch.name, expr)
    return eval(code, {"ceil": math.ceil, "__builtins__": {}}, {n.replace(".", "__"): values.get(n) or 0 for n in names})
