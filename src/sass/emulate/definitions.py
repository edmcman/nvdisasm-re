"""On-demand, pure C++ functions for operators registered by lifting in this process.

Operands and results are bit patterns, with bits above their declared widths zero.
The generated header uses the floating-point backend selected by sass/fp.hpp.
"""
import re
from sass.emulate import cxx
from sass.semantics import bitvec, family


KEYWORDS = set("""alignas alignof and and_eq asm auto bitand bitor bool break case catch char
char8_t char16_t char32_t class compl concept const consteval constexpr constinit const_cast
continue co_await co_return co_yield decltype default delete do double dynamic_cast else enum
explicit export extern false float for friend goto if inline int long mutable namespace new
noexcept not not_eq nullptr operator or or_eq private protected public register reinterpret_cast
requires return short signed sizeof static static_assert static_cast struct switch template this
thread_local throw true try typedef typeid typename union unsigned using virtual void volatile
wchar_t while xor xor_eq""".split())


def nodes(exprs):
    """Distinct expression nodes, including expressions inside selector-arm tuples."""
    seen, stack = set(), list(exprs)
    while stack:
        value = stack.pop()
        if isinstance(value, bitvec.Expr):
            if id(value) in seen: continue
            seen.add(id(value))
            yield value
            stack.extend(value.args)
        elif isinstance(value, (tuple, list)):
            stack.extend(value)


def definition_uses(exprs) -> set[tuple[str, tuple[int, ...]]]:
    """Collect (label, operand widths) without expanding defined operators."""
    out = set()
    for node in nodes(exprs):
        if node.op not in bitvec.DEFINED: continue
        if not all(isinstance(arg, bitvec.Expr) for arg in node.args):
            raise ValueError(f"{node.op}: defined operands must be bitvector expressions")
        out.add((node.op, tuple(arg.w for arg in node.args)))
    return out


def form(label, widths):
    """Validate a registered form and return its C++ name."""
    context = f"{label!r} {widths!r}"
    if not isinstance(label, str) or re.fullmatch(r"[A-Za-z0-9.]+", label) is None:
        raise ValueError(f"{context}: invalid defined operator label")
    name = label.replace(".", "_")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) is None or name in KEYWORDS or "__" in name:
        raise ValueError(f"{context}: invalid C++ function name {name!r}")
    if label not in bitvec.DEFINED or label not in family.DEFINITIONS:
        raise ValueError(f"{context}: unregistered definition; lift its instruction first")
    forms = family.DEFINITIONS[label][1]
    if len(widths) not in forms:
        raise ValueError(f"{context}: unregistered operand count; lift this form first")
    if tuple(widths) != forms[len(widths)][2]:
        raise ValueError(f"{context}: operand widths differ from the registered form {forms[len(widths)][2]!r}")
    for width in widths:
        if type(width) is not int or width < 1:
            raise ValueError(f"{context}: invalid operand width {width!r}")
        if width > cxx.WIDEST:
            raise NotImplementedError(f"{context}: {width}-bit operand (at most {cxx.WIDEST} bits)")
    return name


def definition_name(label: str, widths: tuple[int, ...]) -> str:
    """Unqualified name in sass::def; operand-count variants are overloads."""
    return form(label, widths)


class Lower(cxx.Lower):
    """Pure expression lowering, with generated function arguments as its leaves."""
    def __init__(self, params):
        super().__init__(None)
        self.params = {id(param): param.args[0] for param in params}

    def compute(self, expr):
        if expr.op == "param":
            if id(expr) not in self.params: raise NotImplementedError(f"unbound parameter {expr.args[0]!r}")
            return self.params[id(expr)]
        return super().compute(expr)


def definition_function(label: str, widths: tuple[int, ...]) -> str:
    """Complete SASS_HD inline function in sass::def, expanding over runtime arguments."""
    name = form(label, widths)
    params = [bitvec.param(f"a{k}", width) for k, width in enumerate(widths)]
    param_ids = {id(param) for param in params}
    try:
        expr = bitvec.lowered(bitvec.DEFINED[label].expand(*params))
        if not isinstance(expr, bitvec.Expr): raise NotImplementedError("expansion is not a bitvector expression")
        for node in nodes([expr]):
            if node.op in ("primitive", "reg", "load", "sel"):
                raise NotImplementedError(f"expansion contains {node.op}")
            if node.op == "param" and id(node) not in param_ids:
                raise NotImplementedError(f"unbound parameter {node.args[0]!r}")
            if not 1 <= node.w <= cxx.WIDEST:
                raise NotImplementedError(f"{node.w}-bit {node.op} (at most {cxx.WIDEST} bits)")
        low = Lower(params)
        result = low.value(expr)
    except Exception as error:
        raise NotImplementedError(f"{label!r} {widths!r}: cannot generate pure function: {error}") from error
    args = ", ".join(f"sass::Bits<{width}> a{k}" for k, width in enumerate(widths))
    head = f"SASS_HD inline sass::Bits<{expr.w}> {name}({args}) {{"
    return "\n".join(["namespace sass::def {", head, *("  " + line for line in low.lines),
                      f"  return sass::wrap<{expr.w}>({result});", "}", "}"])


def definitions_header(uses) -> str:
    """Self-contained header, each requested function once, in deterministic order."""
    functions, signatures = [], set()
    for label, widths in sorted(set(uses)):
        name = definition_name(label, widths)
        signature = name, tuple(cxx.ty(width) for width in widths)
        if signature in signatures:
            raise ValueError(f"{label!r} {widths!r}: ambiguous C++ function signature")
        signatures.add(signature)
        functions.append(definition_function(label, widths))
    return "\n\n".join(['#pragma once\n#include "sass/fp.hpp"\n#include "sass/bits.hpp"', *functions]) + "\n"
