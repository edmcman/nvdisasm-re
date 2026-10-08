"""Differential decode: generated SLEIGH (via Ghidra) vs pydecode in SLEIGH display mode.

usage: python3 tests/compare_ghidra.py SM89 [real|synthetic|mutated] [N] [--show K] [--fp exact|readable]
"""
import argparse, json, os, re, subprocess, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]
import compare, oracle
from sass import pydecode

ROOT = Path(__file__).resolve().parent.parent
LDEFS = Path(os.environ.get("SASS_SLEIGH_OUT", ROOT / "processor" / "SASS" / "data" / "languages")) / "sass.ldefs"
GHIDRA_PY = os.environ.get("GHIDRA_PY", os.path.expanduser("~/.config/ghidra/ghidra_12.1.4_PUBLIC/venv/bin/python3"))

def loose(text):
    """Guard as `@P0 `, whitespace inside the operand list ignored (Ghidra spaces operands its own way)."""
    text = re.sub(r"^(@!?\w+):", r"\1 ", text.strip())
    guard, rest = (text.split(" ", 1) + [""])[:2] if text.startswith("@") else ("", text)
    mnem, _, ops = rest.partition(" ")
    ops = re.sub(r"\s+", "", ops)
    # SLEIGH's computed display values are signed int64. These instruction
    # immediates represent the same bits whether printed signed or unsigned.
    if re.match(r"(?:(?:U?MOV)\.64|MOV64IUR)(?:\.|$)", mnem):
        ops = re.sub(r"(?:^|(?<=,))(-?0x[0-9a-fA-F]+)(?=$|\.)",
                     lambda m: hex(int(m[1], 16) & ((1 << 64) - 1)), ops)
    return f"{guard} {mnem} {ops}".strip()

def ghidra(arch, words_addrs, *opts, fp="exact"):
    from sass.gen_sleigh import variant
    lang = f"SASS:LE:64:{variant(arch, fp)}"
    inp = "".join(f"{a:x} {w.hex()}\n" for w, a in words_addrs)
    p = subprocess.run([GHIDRA_PY, str(ROOT / "tests" / "ghidra_decode.py"), str(LDEFS), lang, *opts],
                       input=inp, capture_output=True, text=True, cwd="/")
    lines = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    if len(lines) != len(words_addrs): raise RuntimeError(p.stderr[-3000:])
    return lines

def main():
    p = argparse.ArgumentParser()
    p.add_argument("arch"); p.add_argument("source", nargs="?", default="real")
    p.add_argument("n", nargs="?", type=int, default=20000); p.add_argument("--show", type=int, default=2)
    p.add_argument("--fp", choices=("exact", "readable"), default="exact")
    a = p.parse_args()
    words = compare.sample(a.arch, a.source, a.n)
    res = oracle.Oracle().disasm(a.arch, words)
    pydecode.FLOAT_HEX = True
    todo = []
    for w in words:
        try: d = pydecode.decode(a.arch, w, res[w].addr); todo.append((w, res[w].addr, d.klass.name, d.text))
        except pydecode.NoMatch: pass
    got = ghidra(a.arch, [(w, addr) for w, addr, _, _ in todo], fp=a.fp)
    stat, groups = Counter(), defaultdict(list)
    for (w, addr, cls, exp), g in zip(todo, got):
        s = "error" if "error" in g else "ok" if loose(g["text"]) == loose(exp) else "mismatch"
        stat[s] += 1
        if s != "ok": groups[(s, cls)].append((w, exp, g.get("text") or g.get("error")))
    print(" ".join(f"{k}={v}" for k, v in stat.most_common()), f"pass={stat['ok']}/{len(todo)} classes_failing={len(groups)}")
    for (s, cls), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"\n[{s}] {cls}: {len(items)}")
        for w, exp, g in items[:a.show]: print(f"   {w.hex()}\n     pydecode: {exp}\n     ghidra:   {g}")

if __name__ == "__main__":
    main()
