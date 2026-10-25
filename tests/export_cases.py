"""Export the shared cases of one language as JSON lines, one case per line.

python -m tests.export_cases SM80 [--generated] [--family F ...] [--fp exact|readable] [--sample PERCENT | --exhaustive] [-o cases.jsonl.gz]

Each line has id, arch, fp, family, class, then the case's request keys (word, registers,
memory, observe, and, when present, observe_memory, context, kernel/output), expected (observed
registers and the `__` checks of tests/harness/checks.py) and hardware (the GPU directive, or null).
A path ending in .gz is gzipped; without -o the lines go to stdout.
"""
import argparse, gzip, json, sys
from collections import Counter
from contextlib import nullcontext
from tests.cases import FAMILIES, stream
from tests.harness import sampling, sharded


def record(arch, fp, family, case):
    request = {k: v for k, v in case.request.items() if k != "inspect_pcode"}
    return dict(id=case.id, arch=arch, fp=fp, family=family, **{"class": case.cls}, **request, expected=case.expected, hardware=case.hardware)


def lines(job):
    arch, fp, family, *_ = job
    return family, [json.dumps(record(arch, fp, family, case)) for case in stream(*job)]


def export(out, arch, fp="exact", families=FAMILIES, generated=False, sample=sampling.DEFAULT):
    """Write the language's cases to the text stream `out`; {family: cases}."""
    counts = Counter()
    for family, part in sharded.run(lines, [(arch, fp)], families, sample, **({} if generated else dict(shards=sharded.manual))):
        counts[family] += len(part)
        out.writelines(line + "\n" for line in part)
    return counts


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("arch"); p.add_argument("--fp", default="exact", choices=("exact", "readable"))
    p.add_argument("--family", nargs="+", default=FAMILIES, choices=FAMILIES)
    p.add_argument("--generated", action="store_true", help="also the generated cases of the saved plan")
    p.add_argument("-o", "--output", default="-")
    sampling.add_arguments(p)
    a = p.parse_args()
    match a.output:
        case "-": target = nullcontext(sys.stdout)
        case path if path.endswith(".gz"): target = gzip.open(path, "wt")
        case path: target = open(path, "w")
    with target as out: counts = export(out, a.arch, a.fp, a.family, a.generated, sampling.resolve(a.sample, a.exhaustive))
    print(f"{a.arch} {a.fp}: {counts.total()} cases", *(f"{f} {n}" for f, n in counts.items()), sep="\n  ", file=sys.stderr)


if __name__ == "__main__":
    main()
