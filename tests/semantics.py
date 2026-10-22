"""test_pcode_semantics outside pytest: run emulator cases through generated p-code and report failures.

usage: python -m tests.semantics SM89 [exact|readable] [--family f2fp ...] [--show N] [--sample PERCENT | --exhaustive]
"""
import argparse
from collections import defaultdict
from tests.cases import FAMILIES, emulator_cases
from sass import ghidra_bridge
from tests.harness.checks import problems
from tests.harness import sampling


def failures(arch, fp, families=FAMILIES, results=None, sample=sampling.DEFAULT):
    """(total, {(family, class): [(case, result, problems)]}) for one language."""
    cases = [(family, case) for family, cs in emulator_cases(arch, fp, families, sample).items() for case in cs]
    results = results or ghidra_bridge.emulate_sharded({0: (arch, fp, [case.request for _, case in cases])})[0]
    out = defaultdict(list)
    for (family, case), result in zip(cases, results):
        if found := problems(case.expected, result): out[family, case.cls].append((case, result, found))
    return len(cases), dict(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("arch"); p.add_argument("fp", nargs="?", default="exact", choices=("exact", "readable"))
    p.add_argument("--family", nargs="+", default=FAMILIES, choices=FAMILIES); p.add_argument("--show", type=int, default=2)
    sampling.add_arguments(p)
    a = p.parse_args()
    total, failed = failures(a.arch, a.fp, a.family, sample=sampling.resolve(a.sample, a.exhaustive))
    print(f"{a.arch} {a.fp}: semantics {total - sum(map(len, failed.values()))}/{total}")
    for (family, cls), items in sorted(failed.items()):
        print(f"[{family}] {cls}: {len(items)} failing")
        for case, result, found in items[:a.show]: print(f"   {found}: expected {case.expected}, got {result}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
