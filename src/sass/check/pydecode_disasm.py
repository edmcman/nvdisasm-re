"""Layer 1 report: pydecode vs the nvdisasm oracle.

usage: python -m sass.check.pydecode_disasm SM89 [real|synthetic|mutated] [N] [--show K] [--cls REGEX]
"""
import argparse, random, re
from collections import Counter, defaultdict
from sass.decode import pydecode
from sass.oracle import corpus, nvdisasm as oracle, synth

PASSING = ("ok", "both_err", "collision")  # both_err: both reject the word; collision: see compare_ghidra_disassembly


def normalize(text):
    """Canonical instruction text shared by every side of a comparison."""
    return re.sub(r"\s+", " ", re.sub(r"\s*,\s*", ", ", text.strip().rstrip(";"))).strip()


def sample(arch, source, n, seed=0, real=None):
    """(words, labels): up to n words (all if n == 0); synthetic labels name each word's class."""
    match source:
        case "synthetic": pool = synth.synthetic(arch, 64, seed)
        case "mutated": pool = corpus.mutated(real or corpus.real(arch), n, seed)
        case _: pool = real or corpus.real(arch)
    keys = sorted(pool)
    words = random.Random(seed).sample(keys, min(n, len(keys))) if n else keys
    return words, pool if source == "synthetic" else None


def py_decode(arch, w, addr):
    try:
        d = pydecode.decode(arch, w, addr, conditions=False)  # nvdisasm prints md-illegal words
        return d.klass.name, d.text, None
    except pydecode.NoMatch as e:
        return None, None, str(e)


def compare(arch, words, decoder=py_decode):
    """{word: (status, cls, expected, got)}; status in ok, mismatch, missing (we fail, oracle ok),
    extra (we decode an illegal word), both_err, blank (nvdisasm prints nothing)."""
    out = {}
    for w, r in oracle.Oracle().disasm(arch, words).items():
        cls, got, err = decoder(arch, w, r.addr)
        match (r.text is not None, got is not None):
            case _ if r.text == "": status = "blank"
            case (True, True): status = "ok" if normalize(r.text) == normalize(got) else "mismatch"
            case (True, False): status = "missing"
            case (False, True): status = "extra"
            case _: status = "both_err"
        out[w] = (status, cls, r.text or r.error, got or err)
    return out


def report(out, show=3, cls_re=None, labels=None, sides=("nvdisasm", "pydecode")):
    stat = Counter(s for s, *_ in out.values())
    groups = defaultdict(list)
    for w, (s, cls, exp, got) in out.items():
        if s in PASSING + ("blank",) or cls_re and not re.search(cls_re, f"{cls} {exp}"): continue
        label = labels[w] if labels else cls if s != "missing" else exp.split()[0] if exp else None
        groups[s, label].append((w, exp, got))
    print(" ".join(f"{k}={v}" for k, v in stat.most_common()), f"pass={sum(stat[s] for s in PASSING)}/{len(out) - stat['blank']} classes_failing={len(groups)}")
    for (s, cls), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"\n[{s}] {cls}: {len(items)}")
        for w, exp, got in items[:show]: print(f"   {w.hex()}\n     {sides[0]}: {exp}\n     {sides[1]}: {got}")


def arguments(doc):
    p = argparse.ArgumentParser(description=doc)
    p.add_argument("arch"); p.add_argument("source", nargs="?", default="real", choices=("real", "synthetic", "mutated"))
    p.add_argument("n", nargs="?", type=int, default=20000); p.add_argument("--show", type=int, default=2); p.add_argument("--cls")
    return p


def main():
    a = arguments(__doc__).parse_args()
    words, labels = sample(a.arch, a.source, a.n)
    report(compare(a.arch, words), a.show, a.cls, labels)


if __name__ == "__main__":
    main()
