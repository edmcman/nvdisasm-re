"""Compare committed semantic coverage and archive superseded PR comments.

Uses only the standard library; does not import or regenerate SASS semantics.
"""
import argparse
from collections import Counter
import html
import json
import os
from pathlib import Path
import subprocess
import urllib.parse
import urllib.request


MARKER = "<!-- nvdisasm-re:coverage -->"
OUTDATED_MARKER = "<!-- nvdisasm-re:coverage-outdated -->"
LABEL = "coverage"
COVERAGE = "processor/SASS/semantics/coverage.json"
BADGE = "processor/SASS/semantics/badge.json"
COMMENT_LIMIT = 60_000  # Leave room below GitHub's comment size limit.
FIELDS = (
    ("opcodes", "Classes"),
    ("combination_status", "Modifier combinations"),
    ("opcode_evidence", "Class verification"),
    ("combination_evidence", "Combination verification"),
)
STATUS_COLORS = {
    "native": "🟢", "primitive": "🟡", "opaque": "🔴",
    "partially native": "🟡", "partially primitive": "🟡",
    "gpu": "🔵", "ptxas": "🟤", "reference": "🟢", "none": "⚪",
}
# Best first: a change is judged by the count at that level or better, so evidence moving between
# levels (reference promoted to gpu, or gpu demoted to reference) is neutral for the lower level.
STATUS_ORDER = ("native", "partially native", "primitive", "partially primitive", "opaque")
EVIDENCE_ORDER = ("gpu", "ptxas", "reference", "none")
ORDERS = {"opcodes": STATUS_ORDER, "combination_status": STATUS_ORDER,
          "opcode_evidence": EVIDENCE_ORDER, "combination_evidence": EVIDENCE_ORDER}
EVIDENCE_NAMES = {"gpu": "GPU", "ptxas": "ptxas", "reference": "Reference"}
# Shown when a level of evidence falls: what usually causes it and how to restore it.
EVIDENCE_HINTS = {
    "gpu": "A recorded GPU pass (`tests/baseline/gpu_case_passes.json`, `gpu_passes.json`) stops counting "
           "when its family's cases or its fixture change. Re-run `uv run pytest -m gpu --require-gpu --family F` "
           "on SM89, then `uv run sass-refresh-coverage`.",
    "ptxas": "An annotated `tests/ptx` probe no longer emits these forms.",
    "reference": "Emulator cases for these forms were removed.",
}
LIMIT_NAMES = 12  # opcodes named in a callout
# Where counts come from or go when a total changes: classes are added or removed; combinations
# enter or leave the evidence counts by gaining or losing semantics.
SOURCE = {"opcodes": "absent", "combination_status": "absent",
          "opcode_evidence": "opaque", "combination_evidence": "opaque"}


def counts(values):
    return "<br>".join(f"{STATUS_COLORS.get(k, '⚪')} {html.escape(k).replace('|', '&#124;')}: **{v:,}**"
                      for k, v in sorted(values.items())) or "—"


def trend(order, before, after, kind):
    """+1/-1/0: whether `kind`'s change is an improvement; the last level (opaque, none) is better lower."""
    if kind not in order:
        return 0
    i = order.index(kind)
    levels = order[i:i + 1] if i == len(order) - 1 else order[:i + 1]
    d = sum(after.get(k, 0) - before.get(k, 0) for k in levels) * (-1 if i == len(order) - 1 else 1)
    return (d > 0) - (d < 0)


def tone(t):
    return "🟢" if t > 0 else "🔴" if t < 0 else "⚪"


def ranked(order, kinds):
    return sorted(kinds, key=lambda k: (order.index(k) if k in order else len(order), k))


def deltas(field, before, after):
    """[(kind, delta, trend)] for the kinds whose count changed, best first."""
    order = ORDERS[field]
    kinds = ranked(order, before.keys() | after.keys())
    return [(k, after.get(k, 0) - before.get(k, 0), trend(order, before, after, k))
            for k in kinds if after.get(k, 0) != before.get(k, 0)]


def change(field, before, after):
    return "<br>".join(f"{tone(t)} {html.escape(k)} **{d:+,}**" for k, d, t in deltas(field, before, after)) or "—"


def flows(field, before, after):
    """([(from, to, n)], ambiguous): the fewest moves turning `before`'s counts into `after`'s, matching
    the levels that fell to those that rose best first; ambiguous when another matching fits as well."""
    d = {k: after.get(k, 0) - before.get(k, 0) for k in ranked(ORDERS[field], before.keys() | after.keys())}
    d[SOURCE[field]] = d.get(SOURCE[field], 0) - sum(d.values())
    sources, sinks = [[k, -v] for k, v in d.items() if v < 0], [[k, v] for k, v in d.items() if v > 0]
    out, i, j = [], 0, 0
    while i < len(sources) and j < len(sinks):
        n = min(sources[i][1], sinks[j][1])
        out.append((sources[i][0], sinks[j][0], n))
        sources[i][1] -= n
        sinks[j][1] -= n
        i, j = i + (not sources[i][1]), j + (not sinks[j][1])
    return out, len(sources) > 1 and len(sinks) > 1


def sankey(title, moved, ambiguous):
    links = "\n".join(f"{a} (before),{b} (after),{n}" for (a, b), n in moved.items())
    note = "Flows inferred from the counts, assuming the fewest moves" + (
        f"; others fit {names(ambiguous)} equally" if ambiguous else "") + "."
    return (f"\n**{title}**\n\n```mermaid\n---\nconfig:\n  sankey:\n    showValues: true\n    linkColor: source\n"
            f"---\nsankey-beta\n{links}\n```\n\n{note}\n")


def diagrams(old, new, changed):
    """A Sankey diagram per metric of what moved between levels across the changed opcodes."""
    out = []
    for field, title in FIELDS:
        moved, ambiguous = Counter(), []
        for op in changed:
            links, unclear = flows(field, old.get(field, {}).get(op, {}), new.get(field, {}).get(op, {}))
            for a, b, n in links:
                moved[a, b] += n
            ambiguous += [op] * unclear
        if moved:
            out.append(sankey(title, moved, ambiguous))
    return out


def total(entry, field):
    """{kind: n} summed over the architecture's opcodes."""
    out = {}
    for values in entry.get(field, {}).values():
        for k, v in values.items():
            out[k] = out.get(k, 0) + v
    return out


def at_least(values, order, kind):
    return sum(values.get(k, 0) for k in order[:order.index(kind) + 1])


def opcode_totals(entry):
    # Same definition and rounding as sass.sleigh.coverage.opcode_percent / badge.
    ops = entry.get("opcodes", {})
    covered = sum(set(c) != {"opaque"} for c in ops.values())
    return covered, len(ops), round(100 * covered / len(ops)) if ops else 0


def totals_text(entry):
    if entry is None:
        return "absent"
    covered, total, percent = opcode_totals(entry)
    return f"{covered}/{total} ({percent}%)"


def names(ops):
    shown = ", ".join(html.escape(op) for op in ops[:LIMIT_NAMES])
    return shown + (f" and {len(ops) - LIMIT_NAMES} more" if len(ops) > LIMIT_NAMES else "")


def callouts(compared):
    """GitHub alerts for evidence or coverage that fell, per level and architecture."""
    out, named = [], set()
    for kind in EVIDENCE_NAMES:
        lines = []
        for arch, old, new, changed in compared:
            # An opcode is named once, at the strongest level that fell.
            fell = [op for op in changed if (arch, op) not in named and any(
                at_least(new.get(f, {}).get(op, {}), EVIDENCE_ORDER, kind) < at_least(old.get(f, {}).get(op, {}), EVIDENCE_ORDER, kind)
                for f in ("opcode_evidence", "combination_evidence"))]
            named |= {(arch, op) for op in fell}
            if fell:
                d = [at_least(total(new, f), EVIDENCE_ORDER, kind) - at_least(total(old, f), EVIDENCE_ORDER, kind)
                     for f in ("opcode_evidence", "combination_evidence")]
                lines.append(f"> - **{arch}**: classes **{d[0]:+,}**, combinations **{d[1]:+,}** · "
                             f"{len(fell)} {'opcode' if len(fell) == 1 else 'opcodes'}: {names(fell)}")
        if lines:
            level = "evidence" if kind == "gpu" else "or stronger evidence"
            out.append(f"> [!CAUTION]\n> **{EVIDENCE_NAMES[kind]} {level} decreased**\n>\n"
                       + "\n".join(lines) + f"\n>\n> {EVIDENCE_HINTS[kind]}\n")
    lines = []
    for arch, old, new, changed in compared:
        fell = [op for op in changed if any(t < 0 for f in ("opcodes", "combination_status")
                                            for *_, t in deltas(f, old.get(f, {}).get(op, {}), new.get(f, {}).get(op, {})))]
        if fell:
            lines.append(f"> - **{arch}**: {len(fell)} {'opcode' if len(fell) == 1 else 'opcodes'}: {names(fell)}")
    if lines:
        out.append("> [!WARNING]\n> **Semantic coverage decreased**\n>\n" + "\n".join(lines) + "\n")
    return out


def verification_table(compared):
    """Per-architecture class and combination verification changes, or None when none changed."""
    rows = []
    for arch, old, new, _ in compared:
        cells = []
        for kind in EVIDENCE_ORDER:
            parts = [f"{tone(t)} {what} **{d:+,}**" for what, f in (("classes", "opcode_evidence"), ("combinations", "combination_evidence"))
                     for k, d, t in deltas(f, total(old, f), total(new, f)) if k == kind]
            cells.append("<br>".join(parts) or "—")
        if any(c != "—" for c in cells):
            rows.append(f"| {arch} | " + " | ".join(cells) + " |")
    if not rows:
        return None
    return ("| Architecture | " + " | ".join(f"{STATUS_COLORS[k]} {k}" for k in EVIDENCE_ORDER) + " |\n"
            "| :--- | " + " | ".join("---:" for _ in EVIDENCE_ORDER) + " |\n" + "\n".join(rows) + "\n")


def badge(repo, sha):
    """The README's opcode-coverage badge, read from the PR head's committed badge.json."""
    raw = urllib.parse.quote(f"https://raw.githubusercontent.com/{repo}/{sha}/{BADGE}", safe="")
    report = f"https://raw.githack.com/{repo}/{sha}/{BADGE.replace('badge.json', 'coverage.html')}"
    return f"[![opcode semantics](https://img.shields.io/endpoint?url={raw})]({report})"


def render(base, head, base_sha, head_sha, run_url=None, repo=None):
    """Return the full report, bounded comment, and whether reportable data changed; without a
    repository or run (a local comparison) the badge and CI-run link are omitted."""
    archs = sorted((a for a in base.keys() | head.keys() if "_" not in a),
                   key=lambda a: int(a.removeprefix("SM")))
    rows, sections, compared, moved = [], [], [], False
    for arch in archs:
        before, after = base.get(arch), head.get(arch)
        old, new = before or {}, after or {}
        ops = sorted(old.get("opcodes", {}).keys() | new.get("opcodes", {}).keys())
        changed = [op for op in ops if any(
            old.get(field, {}).get(op) != new.get(field, {}).get(op)
            for field, _ in FIELDS)]
        if before is not None and after is not None and not changed:
            continue
        compared.append((arch, old, new, changed))
        if before is not None and after is not None:
            pp = opcode_totals(after)[2] - opcode_totals(before)[2]
            delta = f"{tone(pp)} {pp:+d} pp"
            moved |= pp != 0
        else:
            delta = "added" if after is not None else "removed"
            moved = True
        rows.append(f"| {arch} | {totals_text(before)} | {totals_text(after)} | {delta} |")
        trends = {op: {t for field, _ in FIELDS for *_, t in deltas(field, old.get(field, {}).get(op, {}), new.get(field, {}).get(op, {}))}
                  for op in changed}
        worse, better = sum(-1 in t for t in trends.values()), sum(-1 not in t and 1 in t for t in trends.values())
        tally = "".join(f" · {tone(s)} {n} {word}" for s, n, word in ((-1, worse, "regressed"), (1, better, "improved")) if n)
        noun = "opcode" if len(changed) == 1 else "opcodes"
        details = [f"<details>\n<summary>{arch} · {len(changed)} changed {noun}{tally}</summary>\n", *diagrams(old, new, changed)]
        # Regressions first, then improvements, then neutral changes.
        for op in sorted(changed, key=lambda op: (0 if -1 in trends[op] else 1 if 1 in trends[op] else 2, op)):
            label = " (added)" if op not in old.get("opcodes", {}) else " (removed)" if op not in new.get("opcodes", {}) else ""
            mark = "🔴 " if -1 in trends[op] else "🟢 " if 1 in trends[op] else ""
            details.append(f"\n{mark}**{html.escape(op)}{label}**\n")
            details.extend(["| Metric | Before | After | Change |", "| :--- | ---: | ---: | :--- |"])
            for field, title in FIELDS:
                b, h = old.get(field, {}).get(op, {}), new.get(field, {}).get(op, {})
                if b != h:
                    details.append(f"| {title} | {counts(b)} | {counts(h)} | {change(field, b, h)} |")
        details.append("\n</details>\n")
        sections.append("\n".join(details))
    header = (f"{MARKER}\n## 🧪 Semantic coverage\n\n" + (f"{badge(repo, head_sha)}\n\n" if repo else "")
              + f"Comparing `{base_sha[:7]}` → `{head_sha[:7]}`" + (f" · [View CI run]({run_url})" if run_url else "") + "\n\n")
    if not rows:
        body = header + "No semantic coverage changes compared with the PR base.\n"
        return {"full": body, "body": body, "changed": False}
    coverage = ("| Architecture | Before | After | Change |\n"
                "| :--- | ---: | ---: | ---: |\n" + "\n".join(rows) + "\n")
    if not moved:
        coverage = "<details>\n<summary>Opcode coverage: ⚪ unchanged</summary>\n\n" + coverage + "\n</details>\n"
    verification = verification_table(compared)
    summary = (header + "".join(c + "\n" for c in callouts(compared))
               + "### Verification changes\n\n" + (verification or "No verification changes.\n") + "\n"
               + ("### Opcode coverage\n\n" if moved else "") + coverage + "\n"
               "<details>\n<summary>How to read this report</summary>\n\n"
               "- **Opcode coverage** counts opcodes with at least one class with semantics, including partial or primitive semantics, "
               "in percentage points (pp).\n"
               "- **Verification** counts use the strongest evidence: GPU, ptxas, reference, or none. "
               "Colors: 🔵 GPU · 🟤 ptxas · 🟢 reference · ⚪ none. "
               "They do not imply hardware verification on other architectures.\n"
               "- **Change** colors: 🟢 better · 🔴 worse · ⚪ neutral. A level is judged by its count together with the stronger levels, "
               "so evidence moving between levels (reference promoted to GPU, or GPU demoted to reference) is ⚪ for the lower level; "
               "more opaque or unverified (none) counts are worse.\n"
               "- **Diagrams** show what moved between levels per architecture, inferred from the counts "
               "assuming the fewest moves; unchanged counts are omitted.\n"
               "- **Detail colors** indicate status: 🟢 native, 🟡 partial or primitive, 🔴 opaque.\n"
               "- Readable variants share the exact variants' coverage and are omitted.\n\n"
               "</details>\n\n### Changed opcodes\n\n")
    full = summary + "\n".join(sections)
    body = full
    if len(full.encode("utf-8")) > COMMENT_LIMIT:
        notice = (f"\nDetails truncated. Download `coverage-comment-report` from the "
                  f"[CI run]({run_url}) for the full Markdown report.\n")
        body = summary
        for section in sections:
            if len((body + section + notice).encode("utf-8")) > COMMENT_LIMIT:
                break
            body += section
        body += notice
    return {"full": full, "body": body, "changed": True}


def read_coverage(sha):
    return json.loads(subprocess.check_output(["git", "show", f"{sha}:{COVERAGE}"], text=True))


def api(method, path, payload=None):
    request = urllib.request.Request(
        os.environ.get("GITHUB_API_URL", "https://api.github.com") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json", "Content-Type": "application/json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def publish(report, repo, number, base_sha, head_sha, request=api):
    root = f"/repos/{repo}"
    # A cancelled/re-run job must not replace a newer comparison.
    pr = request("GET", f"{root}/pulls/{number}")
    if pr["state"] != "open" or pr["head"]["sha"] != head_sha or pr["base"]["sha"] != base_sha:
        print("Skipping obsolete coverage comparison.")
        return
    has_label = any(label["name"] == LABEL for label in pr.get("labels", []))
    if report["changed"] and not has_label:
        request("POST", f"{root}/issues/{number}/labels", {"labels": [LABEL]})
    elif has_label and not report["changed"]:
        request("DELETE", f"{root}/issues/{number}/labels/{LABEL}")
    existing = []
    page = 1
    while True:
        comments = request("GET", f"{root}/issues/{number}/comments?per_page=100&page={page}")
        for comment in comments:
            if (comment["user"]["login"] == "github-actions[bot]"
                    and (comment.get("body") or "").startswith(MARKER)):
                existing.append(comment)
        if len(comments) < 100:
            break
        page += 1
    current = next((comment for comment in existing if comment["body"] == report["body"]), None)
    # Publish first: if archiving fails, a retry finds the current report and
    # finishes archiving without posting another copy.
    if current is None and (existing or report["changed"]):
        request("POST", f"{root}/issues/{number}/comments", {"body": report["body"]})
    for comment in existing:
        if current is not None and comment["id"] == current["id"]:
            continue
        archived = (f"{OUTDATED_MARKER}\n"
                    "> **Outdated:** A newer coverage report below supersedes this comparison.\n\n"
                    "<details>\n<summary>Outdated coverage report</summary>\n\n"
                    + comment["body"].removeprefix(MARKER).lstrip()
                    + "\n\n</details>\n")
        request("PATCH", f"{root}/issues/comments/{comment['id']}", {"body": archived})


def local(rev):
    """The full report comparing `rev`'s committed coverage with the working tree's."""
    head = json.loads((Path(__file__).resolve().parents[2] / COVERAGE).read_text())
    return render(read_coverage(rev), head, rev, "working tree")["full"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("report", "publish", "local"),
                        help="local: print the report comparing REV with the working tree, outside CI")
    parser.add_argument("rev", nargs="?", default="HEAD")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.command == "local":
        print(local(args.rev))
        return
    if args.directory is None:
        parser.error(f"{args.command} requires --directory")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event.get("pull_request", {})
    repo = os.environ["GITHUB_REPOSITORY"]
    if os.environ["GITHUB_EVENT_NAME"] != "pull_request" or pr.get("head", {}).get("repo", {}).get("full_name") != repo:
        print("Coverage comments apply only to same-repository pull requests.")
        return
    base_sha, head_sha = pr["base"]["sha"], pr["head"]["sha"]
    path = args.directory / "report.json"
    if args.command == "report":
        run_url = f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        report = render(read_coverage(base_sha), read_coverage(head_sha), base_sha, head_sha, run_url, repo)
        args.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")
        (args.directory / "coverage-changes.md").write_text(report["full"], encoding="utf-8")
    else:
        publish(json.loads(path.read_text()), repo, event["number"], base_sha, head_sha)


if __name__ == "__main__":
    main()
