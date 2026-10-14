"""Layer 2 report: generated SLEIGH disassembly (via Ghidra) vs pydecode in SLEIGH display mode.

usage: python -m sass.check.ghidra_disasm SM89 [real|synthetic|mutated] [N] [--show K] [--fp exact|readable]
"""
import re
from sass.decode import ir, pydecode
from sass.md import legal
from sass import oracle
from sass.check.pydecode_disasm import arguments, report, sample
from sass import ghidra_bridge


def loose(text):
    """Guard as `@P0 `, whitespace inside the operand list ignored (Ghidra spaces operands its own way)."""
    text = re.sub(r"^(@!?\w+):", r"\1 ", text.strip())
    guard, rest = (text.split(" ", 1) + [""])[:2] if text.startswith("@") else ("", text)
    mnem, _, ops = rest.partition(" ")
    ops = re.sub(r"\s+", "", ops)
    # SLEIGH's computed display values are signed int64. These instruction immediates represent
    # the same bits whether printed signed or unsigned.
    if re.match(r"(?:(?:U?MOV)\.64|MOV64IUR)(?:\.|$)", mnem):
        ops = re.sub(r"(?:^|(?<=,))(-?0x[0-9a-fA-F]+)(?=$|\.)", lambda m: hex(int(m[1], 16) & ((1 << 64) - 1)), ops)
    return f"{guard} {mnem} {ops}".strip()


def compare(arch, words, fp="exact"):
    """{word: (status, cls, pydecode text, Ghidra text or error)} for words pydecode decodes;
    status in ok, mismatch, error. The SLEIGH enforces md CONDITIONS (`legal.operand_errors`):
    their md-illegal words must fail to decode (expected text None). Addresses come from the oracle
    (branch targets are absolute)."""
    addrs = {w: r.addr for w, r in oracle.Oracle().disasm(arch, words).items()}
    pydecode.FLOAT_HEX = True
    todo = []
    for w in words:
        try: d = pydecode.decode(arch, w, addrs[w], conditions=False)
        except pydecode.NoMatch: continue
        todo.append((w, d.klass.name, None if legal.violation(ir.load(arch), d.klass, d.env, hints=False) else d.text))
    def status(exp, g):
        if exp is None: return "ok" if "error" in g else "mismatch"
        return "error" if "error" in g else "ok" if loose(g["text"]) == loose(exp) else "mismatch"
    def judge(todo):
        return {w: (status(exp, g), cls, exp or "(md-illegal: rejected)", g.get("text") or g.get("error"))
                for (w, cls, exp), g in zip(todo, ghidra_bridge.disassemble(arch, fp, [(w, addrs[w]) for w, _, _ in todo]))}
    out = judge(todo)
    # Ghidra caches instruction prototypes by a 32-bit hash of the constructor tree and trusts it
    # entirely (SleighLanguage.parse), so in a long batch a colliding tree reuses another
    # instruction's prototype. A word that decodes correctly in a fresh process hit a collision.
    retry = judge([t for t in todo if out[t[0]][0] != "ok"])
    out.update({w: ("collision", *rest) for w, (s, *rest) in retry.items() if s == "ok"})
    return out


def main():
    p = arguments(__doc__); p.add_argument("--fp", choices=("exact", "readable"), default="exact")
    a = p.parse_args()
    words, labels = sample(a.arch, a.source, a.n)
    report(compare(a.arch, words, a.fp), a.show, a.cls, labels, sides=("pydecode", "ghidra"))


if __name__ == "__main__":
    main()
