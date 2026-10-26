"""Reproducible, subprocess-isolated planner/case/evidence measurements.

Run from a checkout, using its Python environment. No plans or coverage are written.
Example: python research/verification_performance.py plan --repeat 3 --out /tmp/plan.json
"""
import argparse
import json
import os
import resource
import subprocess
import sys
import time
import tempfile
from pathlib import Path


def work(args):
    from sass.paths import LANGUAGES
    from tests.harness import autocases
    started = time.monotonic()
    if args.operation == "plan":
        import tests.autocases
        rows = json.loads((LANGUAGES / f"sass_{args.arch.lower()}_coverage.json").read_text())
        rows = [r for r in rows if r["cls"] in args.classes]
        plan, report = tests.autocases.plan(args.arch, rows)
        counts = dict(report["counts"], forms=len(plan["forms"]))
    elif args.operation == "cases":
        counts = dict(forms=0, cases=0)
        plan = (json.loads((args.plan_dir / f"{args.arch}.json").read_text())
                if args.plan_dir else autocases.saved_plan(args.arch))
        for entry in plan["forms"]:
            if entry["cls"] not in args.classes:
                continue
            kwargs = {"profile_mode": entry["profile_mode"]} if "profile_mode" in entry else {}
            cases = autocases.generate(args.arch, entry["cls"], entry["settings"],
                                      profiles=entry["profiles"], native=entry["native"],
                                      sample=args.sample, **kwargs)
            counts["forms"] += 1
            counts["cases"] += len(cases)
    elif args.operation == "evidence":
        from tests import evidence
        found = evidence.references(tuple(args.architectures), args.sample)
        counts = dict(cases=sum(v[0] for classes in found.values() for v in classes.values()),
                      forms=sum(len(v[1]) for classes in found.values() for v in classes.values()))
    else:
        from tests import evidence
        sys.argv = ["tests.evidence", *args.architectures, "--output", str(args.artifacts)]
        evidence.main()
        entries = [entry for arch in args.architectures
                   for entry in json.loads((args.artifacts / f"sass_{arch.lower()}_evidence.json").read_text()).values()]
        counts = dict(cases=sum(e.get("reference", 0) for e in entries),
                      forms=sum(len(e.get("forms", {}).get("reference", [])) for e in entries))
    usage = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return dict(wall_seconds=time.monotonic() - started,
                cpu_seconds=usage.ru_utime + usage.ru_stime + children.ru_utime + children.ru_stime,
                peak_process_rss_kib=max(usage.ru_maxrss, children.ru_maxrss),
                counts=counts, load=os.getloadavg(), workers=os.cpu_count())


def processes():
    """Read only CPU and memory accounting, never process command lines or environments."""
    out = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            out[int(path.parent.name)] = (int(fields[1]), int(fields[19]),
                                          int(fields[11]) + int(fields[12]), int(fields[21]))
        except (OSError, ValueError, IndexError):
            continue
    return out


def measured(command):
    # A forkserver's worker usage need not reach getrusage(CHILDREN). Sample the
    # entire descendant tree and retain exited processes' last observed CPU time.
    cpu, peak = {}, 0
    with tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen(command, text=True, stdout=output, start_new_session=True)
        while True:
            snapshot = processes()
            live = {process.pid}
            while children := {pid for pid, fields in snapshot.items() if fields[0] in live} - live:
                live |= children
            rss = 0
            for pid in live & snapshot.keys():
                _, started, ticks, pages = snapshot[pid]
                cpu[pid, started] = ticks
                rss += pages * os.sysconf("SC_PAGE_SIZE")
            peak = max(peak, rss)
            if process.poll() is not None:
                break
            time.sleep(0.1)
        output.seek(0)
        text = output.read()
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, text)
    result = json.loads(text.splitlines()[-1])
    result["sampled_tree_cpu_seconds"] = sum(cpu.values()) / os.sysconf("SC_CLK_TCK")
    result["sampled_tree_peak_rss_kib"] = peak // 1024
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "cases", "evidence", "refresh"))
    parser.add_argument("--arch", default="SM89")
    parser.add_argument("--architectures", nargs="+",
                        default=["SM75", "SM80", "SM86", "SM89", "SM90", "SM100", "SM101", "SM103", "SM107", "SM120"])
    parser.add_argument("--classes", nargs="+", default=["ld__sImmOffset", "hfma2__RRR"])
    parser.add_argument("--sample", type=int, choices=(0, 100), default=0)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--artifacts", type=Path, default=Path("/tmp/verification-evidence"))
    parser.add_argument("--plan-dir", type=Path, help="read isolated comparison plans instead of committed plans")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(work(args)))
        return
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    command = [sys.executable, str(Path(__file__).resolve()), args.operation, "--worker",
               "--arch", args.arch, "--sample", str(args.sample),
               "--classes", *args.classes, "--architectures", *args.architectures,
               "--artifacts", str(args.artifacts)]
    if args.plan_dir:
        command += ["--plan-dir", str(args.plan_dir)]
    runs = [measured(command) for _ in range(args.repeat)]
    result = dict(operation=args.operation, arch=args.arch, architectures=args.architectures,
                  classes=args.classes, sample=args.sample, runs=runs)
    if args.plan_dir:
        result['plan_dir'] = str(args.plan_dir)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
