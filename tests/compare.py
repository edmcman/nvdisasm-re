"""Differential decode report: a decoder under test vs the nvdisasm oracle.

usage: python3 tests/compare.py SM89 [real|synthetic|mutated] [N] [--show K] [--cls REGEX]
"""
import argparse, random, re, sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]
import corpus, oracle
from sass import pydecode
from normalize import normalize

def sample(arch, source, n, seed=0):
    words = corpus.real(arch)
    match source:
        case "real": pool = words
        case "mutated": pool = corpus.mutated(words, n, seed)
        case "synthetic":
            import synth
            pool = synth.synthetic(arch, 64, seed)
    keys = sorted(pool)
    return random.Random(seed).sample(keys, min(n, len(keys))) if n else keys

def py_decode(arch, w, addr):
    try:
        d = pydecode.decode(arch, w, addr)
        return d.klass.name, d.text, None
    except pydecode.NoMatch as e:
        return None, None, str(e)

def compare(arch, words, decoder=py_decode):
    """Per word: (status, cls, expected, got). status in ok, mismatch, missing (we fail, oracle ok), extra (we decode illegal), both_err."""
    results = oracle.Oracle().disasm(arch, words)
    out = {}
    for w, r in results.items():
        cls, got, err = decoder(arch, w, r.addr)
        match (r.text is not None, got is not None):
            case _ if r.text == "": status = "blank"
            case (True, True): status = "ok" if normalize(r.text) == normalize(got) else "mismatch"
            case (True, False): status = "missing"
            case (False, True): status = "extra"
            case _: status = "both_err"
        out[w] = (status, cls, r.text or r.error, got or err)
    return out

def report(out, show=3, cls_re=None, labels=None):
    stat = Counter(s for s, *_ in out.values())
    print(" ".join(f"{k}={v}" for k, v in stat.most_common()), f"pass={stat['ok'] + stat['both_err']}/{len(out) - stat['blank']}")
    groups = defaultdict(list)
    for w, (s, cls, exp, got) in out.items():
        if s in ("ok", "both_err", "blank"): continue
        if cls_re and not re.search(cls_re, f"{cls} {exp}"): continue
        label = labels[w] if labels else cls if s != "missing" else exp.split()[0] if exp else None
        groups[(s, label)].append((w, exp, got))
    for (s, cls), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"\n[{s}] {cls}: {len(items)}")
        for w, exp, got in items[:show]:
            print(f"   {w.hex()}\n     nvdisasm: {exp}\n     pydecode: {got}")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("arch"); p.add_argument("source", nargs="?", default="real")
    p.add_argument("n", nargs="?", type=int, default=20000)
    p.add_argument("--show", type=int, default=2); p.add_argument("--cls")
    p.add_argument("--top", type=int, default=40)
    a = p.parse_args()
    words = sample(a.arch, a.source, a.n)
    out = compare(a.arch, words)
    labels = None
    if a.source == "synthetic":
        import synth
        labels = synth.synthetic(a.arch, 64, 0)
    report(out, a.show, a.cls, labels)

if __name__ == "__main__":
    main()
