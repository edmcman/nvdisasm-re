"""Discovery: run GPU fixtures on the SM89 device without asserting, to derive a reference.

Draft a fixture with expected=None in a family's gpu_fixtures(), run this, write the reference
from the observed outputs, then the fixture becomes a regression test in test_refs_against_gpu.

usage: python -m tests.observe FAMILY [--fixture SUBSTRING] [--diff] [--out FILE]
"""
import argparse, json
from tests.cases import FAMILIES, gpu_fixtures
from tests.harness.gpu import Driver


def main():
    p = argparse.ArgumentParser()
    p.add_argument("family", choices=FAMILIES); p.add_argument("--fixture", default="")
    p.add_argument("--diff", action="store_true", help="show threads where the GPU differs from the current reference")
    p.add_argument("--out", help="write {fixture: [outputs]} as JSON")
    a = p.parse_args()
    fixtures = [f for _, f in gpu_fixtures((a.family,)) if a.fixture in f.name]
    driver, observed = Driver(), {}
    try:
        for f in fixtures:
            gpu = observed[f.name] = driver.run(f)
            print(f"{f.name}: {len(gpu)} outputs, first {[hex(x) for x in gpu[:4]]}")
            if a.diff and f.expected is not None:
                for i, (g, w) in enumerate(zip(gpu, f.expected)):
                    if not f.same(g, w): print(f"   thread {i}: inputs {f.vectors[i] if f.vectors else '?'} gpu {g:#x} reference {w:#x}")
    finally: driver.close()
    if a.out: open(a.out, "w").write(json.dumps(observed, indent=1) + "\n")


if __name__ == "__main__":
    main()
