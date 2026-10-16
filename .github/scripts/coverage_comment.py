"""Compare committed semantic coverage and maintain one same-repository PR comment.

Uses only the standard library; does not import or regenerate SASS semantics.
"""
import argparse
import html
import json
import os
from pathlib import Path
import subprocess
import urllib.request


MARKER = "<!-- nvdisasm-re:coverage -->"
COVERAGE = "processor/SASS/semantics/coverage.json"
COMMENT_LIMIT = 60_000  # Leave room below GitHub's comment size limit.
FIELDS = (
    ("opcodes", "Classes"),
    ("combination_status", "Modifier combinations"),
    ("opcode_evidence", "Class verification"),
    ("combination_evidence", "Combination verification"),
)


def counts(values):
    return ", ".join(f"{html.escape(k)}: {v:,}" for k, v in sorted(values.items())) or "—"


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


def render(base, head, base_sha, head_sha, run_url):
    """Return the full report, bounded comment, and whether reportable data changed."""
    archs = sorted((a for a in base.keys() | head.keys() if "_" not in a),
                   key=lambda a: int(a.removeprefix("SM")))
    rows, sections = [], []
    for arch in archs:
        before, after = base.get(arch), head.get(arch)
        old, new = before or {}, after or {}
        ops = sorted(old.get("opcodes", {}).keys() | new.get("opcodes", {}).keys())
        changed = [op for op in ops if any(
            old.get(field, {}).get(op) != new.get(field, {}).get(op)
            for field, _ in FIELDS)]
        if before is not None and after is not None and not changed:
            continue
        delta = (f"{opcode_totals(after)[2] - opcode_totals(before)[2]:+d} pp"
                 if before is not None and after is not None else "added" if after is not None else "removed")
        rows.append(f"| {arch} | {totals_text(before)} | {totals_text(after)} | {delta} |")
        details = [f"<details>\n<summary>{arch}: {len(changed)} changed opcodes</summary>\n"]
        for op in changed:
            label = " (added)" if op not in old.get("opcodes", {}) else " (removed)" if op not in new.get("opcodes", {}) else ""
            details.append(f"\n**{html.escape(op)}{label}**\n")
            for field, title in FIELDS:
                b, h = old.get(field, {}).get(op, {}), new.get(field, {}).get(op, {})
                if b != h:
                    details.append(f"- {title}: {counts(b)} → {counts(h)}")
        details.append("\n</details>\n")
        sections.append("\n".join(details))
    header = (f"{MARKER}\n## Semantic coverage changes\n\n"
              f"Base `{base_sha}` → head `{head_sha}` · [CI run]({run_url})\n\n")
    if not rows:
        body = header + "No semantic coverage changes compared with the PR base.\n"
        return {"full": body, "body": body, "changed": False}
    summary = (header + "| Architecture | Before | After | Change |\n"
               "| --- | --- | --- | --- |\n" + "\n".join(rows) + "\n\n"
               "Opcode coverage counts opcodes with at least one class with semantics, including partial or primitive semantics. "
               "Verification counts are by strongest evidence (GPU, ptxas, reference, or none); "
               "they do not imply hardware verification on other architectures. "
               "Readable variants share the exact variants' coverage and are omitted.\n\n")
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
    existing = None
    page = 1
    while True:
        comments = request("GET", f"{root}/issues/{number}/comments?per_page=100&page={page}")
        for comment in comments:
            if (comment["user"]["login"] == "github-actions[bot]"
                    and (comment.get("body") or "").startswith(MARKER)):
                existing = comment
                break
        if existing or len(comments) < 100:
            break
        page += 1
    if existing:
        if existing["body"] != report["body"]:
            request("PATCH", f"{root}/issues/comments/{existing['id']}", {"body": report["body"]})
    elif report["changed"]:
        request("POST", f"{root}/issues/{number}/comments", {"body": report["body"]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("report", "publish"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
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
        report = render(read_coverage(base_sha), read_coverage(head_sha), base_sha, head_sha, run_url)
        args.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")
        (args.directory / "coverage-changes.md").write_text(report["full"], encoding="utf-8")
    else:
        publish(json.loads(path.read_text()), repo, event["number"], base_sha, head_sha)


if __name__ == "__main__":
    main()
