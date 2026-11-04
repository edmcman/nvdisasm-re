import argparse
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

from sass import paths
from sass.semantics.family import roles
from sass.sleigh import gen, modifiers, verification
from sass.sleigh.build import ARCHES
from tests.harness import autocases

OPS = ("HMMA", "LDSM", "LDGSTS", "LDGDEPBAR", "DEPBAR", "ATOMG",
       "MEMBAR", "BRX", "B2R", "CCTL", "ERRBAR", "YIELD")


def tool(path):
    path = Path(path)
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                version=subprocess.check_output([path, "--version"], text=True).strip())


def inventory(arch, evidence):
    g = gen.Gen(arch, coverage=True)
    for k in g.arch.classes:
        if k.mnemonic in OPS and not k.alternate:
            g.build(k)
    out = []
    planned = {}
    for entry in autocases.saved_plan(arch)["forms"]:
        planned.setdefault(entry["cls"], set()).add(tuple(sorted(entry["settings"].items())))
    for row in g.coverage.values():
        ev = modifiers.expand(evidence.get(row["cls"], {}))
        counts = Counter()
        for *_, status in row["combinations"]["table"]:
            counts[status] += row["combinations"]["free"]
        names = row["combinations"]["labels"]
        def complete(settings):
            return tuple(sorted((name, settings.get(name, labels[0] if len(labels) == 1 else None))
                                for name, labels in names.items()))
        selected = {complete(dict(form)) for form in planned.get(row["cls"], ())}
        verified = {kind: {complete(dict(zip(ev.get("modifiers", []), labels))) for labels in forms}
                    for kind, forms in ev.get("verification_forms", {}).items()}
        for flags, forms in ev.get("forms", {}).items():
            for kind in set(flags.split("+")) - {"reference"}:
                verified.setdefault(kind, set()).update(
                    complete(dict(zip(ev.get("modifiers", []), labels))) for labels in forms)
        gaps = Counter()
        for form, status in verification.forms(row):
            partial = status in ("partially native", "partially primitive")
            if status not in verification.SUPPORTED and not partial: continue
            key = complete(form)
            prefix = "partial_" if partial else ""
            gaps[prefix + "supported"] += 1
            gaps[prefix + "planned"] += key in selected
            gaps[prefix + "unplanned"] += key not in selected
            for kind in ("reference", "gpu", "ptxas"):
                gaps[prefix + kind] += key in verified.get(kind, ())
        out.append(dict(row, operands=roles(arch).get(row["cls"], {}).get("operands", []),
                        legal_forms=sum(counts.values()), semantics=dict(counts),
                        full_form_verification=dict(gaps),
                        evidence={k: v for k, v in ev.items() if k in
                                  ("modifiers", "forms", "verification_forms", "gpu", "ptxas", "reference")}))
    return out


def report(data):
    rows = [r for rs in data["architectures"].values() for r in rs]
    total = sum(r["legal_forms"] for r in rows)
    replay = json.loads((paths.ROOT / "research/additional-opcodes/yolo-replay.json").read_text())
    replay_counts = replay["summary"]
    executed = Counter(op for launch in replay["launches"] if launch["status"] == "pass"
                       for op in launch.get("executed_targets", {}))
    comparisons = sum(len(launch["comparisons"]) for launch in replay["launches"]
                      if launch["status"] == "pass")
    text = [
        "# Additional opcodes in #117, 2026-11-04", "",
        f"The request remains incomplete. The corrected machine descriptions define {total:,} "
        "non-alias selector forms for these twelve opcodes across ten architectures. "
        "The inventory records each class's legal combination table, operand spans, semantics "
        "and evidence separately. Register values and scheduling fields are additional domains; "
        "these counts do not establish exhaustive input or scheduling coverage.", "",
        "Hardware evidence here is SM89. Other architectures have code generation and independent "
        "reference cases for implemented forms. Compiler evidence applies only to successfully "
        "compiled probes on their recorded targets; rejected probes remain gaps. Neither compiler "
        "traces nor raw discovery captures earn functional verification credit.", "",
        "Supported counts include native semantics and runtime primitives. Reference/GPU counts "
        "below require a complete recorded selector form with positive verification evidence. "
        "Unplanned counts measure every supported full form beyond the planner's interaction "
        "coverage. Sparse and indexed HMMA, packed LDSM, descriptor variants and branch-depth "
        "forms remain visible in the inventory even when unsupported.", "",
        "Partial counts require operand restrictions and remain included in Unresolved. "
        "Reference and GPU columns show complete/partial form counts separately. Dense HMMA "
        "cases exercise accumulator predicate values and inversion as well as source negation.", "",
    ]
    for op in sorted(OPS):
        text += [f"## {op}", "",
                 "| Architecture | Legal | Supported | Partial | Unresolved | Reference | GPU | Unplanned |",
                 "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for arch, rs in data["architectures"].items():
            selected = [r for r in rs if r["opcode"] == op]
            legal = sum(r["legal_forms"] for r in selected)
            counts = Counter()
            for row in selected: counts.update(row["full_form_verification"])
            text.append(f"| {arch} | {legal:,} | {counts['supported']:,} | {counts['partial_supported']:,} | "
                        f"{legal - counts['supported']:,} | {counts['reference']:,}/{counts['partial_reference']:,} | "
                        f"{counts['gpu']:,}/{counts['partial_gpu']:,} | {counts['unplanned']:,} |")
        text.append("")
    text += [
        "## Reproduction", "",
        "Run from the repository root; builds and GPU validation require device/JDK access outside "
        "the sandbox.", "",
        "```bash",
        "nice uv run sass-build-languages --coverage",
        "nice uv run python -m tests.autocases SM107 --check",
        "nice uv run pytest tests/test_lifted_semantics.py tests/test_family_lowering.py "
        "--family atomic --family asynchronous --family matrix --family control --arch all --fp all",
        "nice uv run pytest tests/test_pcode_semantics.py tests/test_fixtures_emulated.py "
        "--family atomic --family asynchronous --family matrix --family control "
        "-m ghidra --arch all --fp all",
        "nice uv run pytest tests/test_cases_on_gpu.py tests/test_refs_on_gpu.py "
        "--family atomic --family asynchronous --family matrix --family control -m gpu --require-gpu",
        "nice uv run sass-refresh-coverage",
        "nice uv run python -m research.additional_opcodes "
        "--out research/additional-opcodes/inventory-current.json "
        "--report docs/additional-opcodes-2026-11-04.md",
        "```", "",
        "Use `--expand` on the inventory command to materialize every legal form rather than its "
        "compact combination table. Per-architecture planner gaps are retained as "
        "`research/additional-opcodes/plan-gaps-SMxx.json`.", "",
        "Numerical and cooperative findings, including unfinished behavior, are documented in "
        "[hardware-facts.md](hardware-facts.md), [emulation.md](emulation.md) and "
        "[open.md](open.md). Compiler identities and hashes are pinned in the inventory. "
        "PTX probes are retained under `tests/ptx/`.", "",
        "Architecture-specific compiler traces and transformed source files are retained in "
        "`research/additional-opcodes/compiler-probes.json` and `compiler-sources/`. Reproduce with "
        "`nice uv run python -m research.compiler_additional_opcodes "
        "--out research/additional-opcodes/compiler-probes.json "
        "--sources research/additional-opcodes/compiler-sources`. This distinguishes emitted "
        "encodings from compiler/version rejections; numerical behavior remains separately evidenced.", "",
        "## Replay and PR checks", "",
        f"The retained YOLO replay compares every captured memory region: {replay_counts.get('pass', 0)} "
        f"launches pass, {replay_counts.get('blocked', 0)} are blocked, "
        f"{replay_counts.get('mismatch', 0)} differ, and {replay_counts.get('uncompared', 0)} "
        f"have no memory region to compare. Passing launches compare {comparisons} complete "
        "memory regions. Target instructions executed by passing launches: "
        + (", ".join(f"{op} in {count} launches" for op, count in sorted(executed.items()))
           or "none")
        + ". Executed counts in each launch record count distinct visited instruction locations, "
        "rather than dynamic invocations. See `research/additional-opcodes/yolo-replay.json`.", "",
        "PR #119's failing logs identified a stale SM107 case plan and launch tests that still "
        "expected Ghidra to reject launch scope. The regenerated plan passes `--check`; the "
        "updated launch test compares against the reference and passes locally on SM89/SM90. "
        "After push `12ee374`, CI exposed invalid size selectors admitted by span constructors "
        "and excess P-code for discarded integer predicates. Span constraints now encode their "
        "narrowed raw domains, and predicate-sharing costs account for narrowing surviving "
        "expressions. Scalar half FMA sharing keeps all twenty languages below the unchanged "
        "16 MiB limit. All-architecture decoder/dataflow and int_alu/fp16 numerical checks passed "
        "2,794 tests with 209,716 selected shared cases; the twenty reviewed operation-count "
        "baselines pass separately. Carry, threads and memory checks on SM89/SM107 in both "
        "modes passed 614 tests with 23,406 shared cases. That refresh left the committed "
        "coverage/evidence files unchanged. Following `d715824`, all 58 current PR checks pass "
        "and nine are skipped. SM90's earlier generation job failed artifact upload "
        "with ECONNRESET after its generation checks passed.", "",
        "The dense TF32 and operand-control extension passes 4,612 SM89 shared GPU cases and "
        "nine whole-warp GPU fixtures. All-architecture exact/readable matrix Ghidra validation "
        "passes 94 tests with 78,216 shared cases; FP16 numerical/dataflow validation passes "
        "916 tests with 155,492 shared cases. Corrected arithmetic passes 72 Python/native tests, "
        "including all eight source-negation/predicate-inversion combinations in the native engine. "
        "Integrated source checks pass 54 tests, and all twenty reviewed P-code baselines pass. "
        "All twenty languages compile below the unchanged 16 MiB limit; SM107 exact is "
        "16,753,273 bytes. Full-form HMMA planning is exhaustive; input profiles remain sampled "
        "regression domains. F16 destinations, sparse/indexed HMMA and other unresolved "
        "target forms still prevent completion.", "",
        "The BF16 extension passes 5,892 SM89 shared GPU cases and twelve whole-warp GPU "
        "fixtures. Final all-architecture exact/readable Ghidra matrix validation passes "
        "106 tests with 99,880 shared cases in 408 seconds. The expanded source-overlap "
        "checks pass 96 Python/native tests; cooperative fixtures separately pass all "
        "six SM89/SM90 schedule combinations. Integrated source checks pass 575 tests, "
        "including real numeric-definition parameter expansion. All twenty reviewed P-code baselines pass. "
        "The shared packed-format parameter increases F16's P-code, while keeping every "
        "language below the unchanged 16 MiB limit (SM107 exact: 16,776,587 bytes). "
        "SM89 has 32 supported dense selector forms with exhaustive planning; F16 "
        "destinations, sparse/indexed forms and other target gaps still prevent completion.", "",
    ]
    return "\n".join(text)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--arch", nargs="+", choices=ARCHES, default=ARCHES)
    p.add_argument("--expand", action="store_true")
    p.add_argument("--report", type=Path)
    a = p.parse_args()
    evidence = json.loads(paths.EVIDENCE.read_text())
    data = dict(tools={n: tool(getattr(paths, n)) for n in ("PTXAS", "NVDISASM")},
                architectures={arch: inventory(arch, evidence.get(arch, {})) for arch in a.arch})
    if a.expand:
        for rows in data["architectures"].values():
            for row in rows:
                row["full_forms"] = [dict(settings=f, semantics=s) for f, s in verification.forms(row)]
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(data, indent=1) + "\n")
    if a.report: a.report.write_text(report(data))
    for arch, rows in data["architectures"].items():
        print(arch, {op: sum(r["legal_forms"] for r in rows if r["opcode"] == op) for op in OPS})


if __name__ == "__main__":
    main()
