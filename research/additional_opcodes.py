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
        "The second trace set, `compiler-probes-13.4.json` and `compiler-sources-13.4/`, uses "
        "ptxas 13.4.92 with PTX 9.4. All thirteen probes compile on nine modeled architectures; "
        "SM75 compiles six. The SM101 model explicitly maps to compiler target `sm_110`, "
        "following the PTX target rename. Package and executable hashes are retained in "
        "`compiler-13.4-identity.json`. With that executable selected through `PTXAS`, reproduce "
        "using `nice uv run python -m research.compiler_additional_opcodes --ptx-version 9.4 "
        "--renamed-targets --out research/additional-opcodes/compiler-probes-13.4.json "
        "--sources research/additional-opcodes/compiler-sources-13.4`. Target mappings and "
        "compiler emission provide no hardware verification credit.", "",
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
        "PR #119's failures came from a stale SM107 case plan, launch tests still expecting "
        "Ghidra to reject launch scope, invalid size selectors admitted by span constructors, "
        "and excess P-code for discarded integer predicates. The plan is regenerated; launch "
        "tests compare against the reference on SM89/SM90; span constructors enforce their "
        "narrowed domains; predicate-sharing costs account for surviving expressions. "
        "Pushed commit `e925a82` finishes with 52 passing checks, nine skipped and six Python "
        "jobs cancelled at the 60-minute timeout. In SM89's job, definition discovery scans "
        "all twenty architecture/mode combinations for 1,274 seconds; barrier checks also "
        "repeat every architecture inside each matrix job. The fix makes explicit "
        "`--arch` selection constrain this work while retaining all architectures across the "
        "CI matrix and preserving unqualified or `--arch all` runs. It does not increase "
        "the timeout. Focused selection validation passes 128 tests with 173 unselected-architecture "
        "skips; both discovery-contract checks and three selected matrix checks pass. An SM90 "
        "selection excludes all 152 SM89-only matrix checks. Subsequent commits require fresh "
        "CI results.", "",
        "## Current matrix validation", "",
        "Direct dense HMMA covers every legal selector form in `hmma_x8_` on SM80 and newer: "
        "F16/F32, BF16/F32, F16/F16 and TF32/F32, plus E6M9/F32 on SM86/SM89/SM90. "
        "SM75 remains unresolved. SM89 hardware regression passes 11,844 matrix shared "
        "cases across all eighty supported dense selector forms, plus thirty "
        "whole-warp fixtures. Raw discovery "
        "observations are retained separately and earn no verification credit. "
        "All-architecture exact/readable Ghidra validation passes numerical and dataflow "
        "checks for matrices: 180,000 shared cases and 200 passing tests, with eighteen "
        "reviewed HMMA-only operation-count baseline changes. The updated "
        "twenty baseline tests pass. Focused source checks pass 216 tests, reference/plan "
        "checks pass 105 tests, and native matrix checks pass 82 tests. Real parameter "
        "expansion passes for all discovered definition signatures across architectures "
        "and both floating-point modes. Every F16 accumulator encoding agrees with the independent "
        "conversion reference. Shared stages and conversions keep all twenty "
        "languages below 16 MiB (SM107 exact after CCTL.D.IVALL: 16,735,261 bytes). The shared fraction-width "
        "calculation reduces sampled HMMA operation counts by 13.9–15.1% against "
        "`4e99cda`. Full-form HMMA planning "
        "is exhaustive; input profiles remain sampled regression domains. F16 output "
        "rounds the aligned sum directly to half after each tensor stage; a distinguishing "
        "SM89 boundary rejects intermediate F32 rounding. SM75 compiler "
        "emission establishes the two F16 `1688` encodings, but its numerical "
        "behavior remains unresolved. Other architectures' numerical staging "
        "remains inferred; sparse/indexed forms and other target gaps remain "
        "unfinished.", "",
        "## Cache-control implementation", "",
        "Ordinary D-cache PF1/PF2 requests are supported on every architecture; PML2/DML2 "
        "are supported on SM80 and newer. Both E and noE address forms are planned "
        "exhaustively for the address subset: four forms on SM75 and eight on each other "
        "architecture. Runtime state records each operation and address and completes "
        "requests immediately under the existing sequentially consistent model. "
        "DML2 requires 128-byte alignment. Compiler traces, source-independent references "
        "and lane-distinguishing before/after memory fixtures establish the supported "
        "requests. GPU regression checks include every profile input beyond the planner's "
        "interaction coverage and twelve two-block predication fixtures. Synthetic noE "
        "and high-bit addresses remain emulator-only; their arithmetic is inferred. "
        "Cache-selector U/C/I, descriptors, predicate outputs, other whole-cache controls, "
        "WB/IV/RS/RML2 and the legal SM75 priority selectors remain unresolved. "
        "No physical cache residency or timing model is claimed.", "",
        "CCTL.D.IVALL adds one supported form on every architecture. Eighty pinned "
        "compiler variants isolate its insertion for GPU/system acquire loads. A "
        "cross-block warm-up and release/acquire probe distinguishes it: all 4,416 "
        "consumers read the new payload with invalidation and stale zero with NOP "
        "or a false guard, despite observing the release flag. Three independent "
        "two-block SASS regressions pass on SM89 with unconditional, predicated and "
        "inverted-predicate invalidation. The runtime records each enabled request "
        "and advances an internal generation; all four predicate truth/inversion "
        "pairs are profiled. Discovery captures remain separate from verification. "
        "See `cache-invalidation-13.4.json`, `cache-invalidation-cross-SM89.json` "
        "and `tests/harness/cache_controls.py`.", "",
        "Final invalidation validation passes 66 source checks, including native generation "
        "and predicate transitions and cross-block schedule variants. Ghidra passes 1,509 "
        "tests and 6,580 shared control cases across twenty languages, including all "
        "operation-count baselines without updates. The exhaustive SM89 control suite "
        "passes 160 tests. The frozen input plans add exactly one D.IVALL form on each "
        "architecture and remove none. Retained replay still compares 470 memory regions "
        "exactly across 249 passing launches, with the same 65 blockers.", "",
        "The final control/conversion Ghidra run passes 4,508 numerical, profile, kernel "
        "and dataflow tests covering 191,390 shared cases across twenty languages. "
        "All twenty reviewed operation-count baselines pass separately. The shared GPU "
        "memory, atomic and asynchronous harness passes 5,533 hardware scenarios after "
        "correcting signed pointer relocation; negative 32-bit offsets previously became "
        "positive offsets near 4 GiB. High-bit Ghidra addresses now cross the Java "
        "signed-long boundary as bit patterns. These fixes retain exact comparisons.", "",
        "## Barrier-state implementation", "",
        "Cooperative B2R discovery retains actual kernels and raw outputs in "
        "`b2r-state-SM89.json`, `b2r-staggered-SM89.json`, "
        "`b2r-reductions-SM89.json`, `b2r-result-modes-SM89.json`, "
        "`b2r-exited-SM89.json`, `b2r-pending-values-SM89.json`, "
        "`b2r-and-counts-SM89.json` and `b2r-arrival-results-SM89.json`. "
        "BAR mode exposes a packed pending state. "
        "Separate fields count arrived warps and accumulated predicates (false for AND, "
        "true otherwise); implicit barriers encode an expected count of zero. Partial-warp "
        "barriers return actual thread counts, and WARP reads the low eleven RESULT bits: "
        "a true Boolean reduction returns RESULT=0xffffffff and WARP=0x7ff. "
        "All three B2R modes are implemented. The shared scheduler propagates ordinary "
        "and reduction results, completes counted generations between warp turns and "
        "models nonblocking ARV. Earlier ARV participants retain their prior result; "
        "blocking/deferred participants and the completing warp receive the new result. "
        "SM89 hardware passes 118 new barrier fixtures and thirteen existing reductions. "
        "Raw captures earn no verification credit. Packed-register producers and "
        "overlapping generations remain unfinished; other architectures' state layouts "
        "are inferred. Reproduce discovery using `nice uv run python -m research.barrier_state_observe "
        "--extended --out /tmp/b2r-state.json`.", "",
        "All-architecture exact/readable lifted validation passes 1,414 tests and 6,236 "
        "shared control cases. Python/native barrier, reconvergence and matrix checks pass "
        "88 tests; forty-two fresh checks after the final snapshot optimization and six "
        "async schedule checks also pass. Ghidra passes 2,008 tests and 6,336 shared cases. "
        "Twenty reviewed operation-count changes add only the WARP result mask; the "
        "coverage refresh and updated baseline checks pass. A final run after the snapshot "
        "optimization passes both barrier Ghidra checks and refreshed coverage in 323 seconds; "
        "the twenty operation-count checks pass without baseline updates in nine seconds. "
        "GPU launch, reference and CUDA "
        "protocol checks pass 148 tests, including 149 shared control scenarios. The "
        "flat-buffer launch helpers now use their explicit geometry instead of requiring "
        "the ordinary per-thread fixture record format.", "",
        "On thirteen identical reduction kernels, a CPU-affinity-pinned process-time "
        "comparison against `292a5f0` took 29.01 versus 29.53 CPU seconds for 100 Python "
        "repetitions, and 1.68 versus 1.46 CPU seconds for 1,000 native repetitions. "
        "The request digest is "
        "`9d26e0f66dce4d4849dc0ddfee298c8531c3cb1674645be424c0e4e32f13ce80`. "
        "Barrier snapshots are produced only when an instruction reads BAR state.", "",
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
