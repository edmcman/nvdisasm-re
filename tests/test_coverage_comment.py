"""CI coverage comparisons and sticky comments, without GitHub writes."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("coverage_comment", ROOT / ".github/scripts/coverage_comment.py")
coverage_comment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(coverage_comment)


def summary():
    return {"SM89": {
        "opcodes": {"FADD": {"native": 1}, "LDG": {"opaque": 1}},
        "combination_status": {"FADD": {"native": 8}, "LDG": {"opaque": 8}},
        "opcode_evidence": {"FADD": {"reference": 1}},
        "combination_evidence": {"FADD": {"reference": 2, "none": 6}},
    }}


def render(before, after):
    return coverage_comment.render(before, after, "base", "head", "https://example.com/run", "owner/repo")


def test_unchanged_and_readable_duplicates():
    base = summary()
    head = copy.deepcopy(base)
    head["SM89_readable"] = {"opcodes": {"OTHER": {"native": 1}}}
    report = render(base, head)
    assert not report["changed"]
    assert "No evidence coverage or modeled support changes" in report["body"]
    assert "## 🧪 Evidence coverage" in report["body"]


def test_evidence_and_modeled_support_have_separate_details_and_trends():
    before, after = gpu_demoted()
    after["SM89"]["opcodes"]["FADD"] = {"native": 2}
    body = render(before, after)["body"]
    evidence, support = body.split("### Changed evidence\n\n", 1)[1].split("### Semantic coverage changes\n\n")
    assert "🔴 **FADD**" in evidence and "🟢 **FADD**" not in evidence
    assert "| Class evidence |" in evidence and "| Classes |" not in evidence
    assert "🟢 **FADD**" in support and "🔴 **FADD**" not in support
    assert "| Classes |" in support and "| Class evidence |" not in support


@pytest.mark.parametrize("reverse", [False, True])
def test_coverage_gain_and_regression(reverse):
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["opcodes"]["LDG"] = {"partially native": 1}
    if reverse:
        before, after = after, before
    report = render(before, after)
    assert report["changed"]
    assert ("🔴 -50 pp" if reverse else "🟢 +50 pp") in report["body"]
    assert "1/2 (50%)" in report["body"]
    assert "2/2 (100%)" in report["body"]
    assert "**LDG**" in report["body"]
    assert "**FADD**" not in report["body"]
    assert "| Metric | Before | After |" in report["body"]
    tally = "🔴 1 regressed" if reverse else "🟢 1 improved"
    assert f"<summary>SM89 · 1 changed opcode · {tally}</summary>" in report["body"]
    assert ("🔴 **LDG**" if reverse else "🟢 **LDG**") in report["body"]
    assert "<summary>How to read this report</summary>" in report["body"]
    assert "### Opcode coverage" in report["body"]
    assert ("[!WARNING]" in report["body"]) == reverse
    assert "[!CAUTION]" not in report["body"]


@pytest.mark.parametrize("field,label", coverage_comment.FIELDS[1:])
def test_combination_and_verification_only_changes(field, label):
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"][field]["FADD"] = {"gpu": 3}
    report = render(before, after)
    assert report["changed"]
    assert "⚪ +0 pp" in report["body"]
    assert f"| {label} |" in report["body"]
    assert "🔵 gpu: **3**" in report["body"]
    assert "<summary>Opcode coverage: ⚪ unchanged</summary>" in report["body"]
    assert "### Opcode coverage" not in report["body"]


def gpu_demoted():
    before = summary()
    before["SM89"]["opcode_evidence"]["FADD"] = {"gpu": 1}
    before["SM89"]["combination_evidence"]["FADD"] = {"gpu": 2, "none": 6}
    return before, summary()


def test_gpu_evidence_drop_is_called_out():
    report = render(*gpu_demoted())
    body = report["body"]
    assert body.index("[!CAUTION]") < body.index("### Changed evidence")
    assert "**GPU evidence decreased**" in body
    assert "**SM89**: classes **-1**, combinations **-2** · 1 opcode: FADD" in body
    assert "pytest -m gpu" in body
    # The demotion to reference is neutral: nothing became less verified than reference.
    assert "🔴 gpu **-1**<br>⚪ reference **+1**" in body
    assert "or stronger evidence decreased" not in body
    assert "| SM89 | 🔴 classes **-1**<br>🔴 combinations **-2** | — | ⚪ classes **+1**<br>⚪ combinations **+2** | — |" in body
    assert "🔴 **FADD**" in body


def test_gpu_evidence_gain_is_green():
    after, before = gpu_demoted()
    body = render(before, after)["body"]
    assert "[!CAUTION]" not in body
    assert "🟢 gpu **+1**<br>⚪ reference **-1**" in body
    assert "· 🟢 1 improved</summary>" in body


def test_lost_verification_names_the_strongest_level_once():
    before, after = gpu_demoted()
    after["SM89"]["opcode_evidence"]["FADD"] = {"none": 1}
    after["SM89"]["combination_evidence"]["FADD"] = {"none": 8}
    body = render(before, after)["body"]
    assert body.count("[!CAUTION]") == 1
    assert "🔴 none **+1**" in body


def test_regressions_sort_first_and_callouts_truncate_names():
    ops = [f"OP{i:02}" for i in range(20)] + ["AAA"]
    before = {"SM89": {"opcodes": {op: {"native": 1} for op in ops},
                       "opcode_evidence": {op: {"none" if op == "AAA" else "gpu": 1} for op in ops}}}
    after = copy.deepcopy(before)
    after["SM89"]["opcode_evidence"] = {op: {"reference": 1} for op in before["SM89"]["opcode_evidence"]}
    body = render(before, after)["body"]
    assert "20 opcodes: OP00, OP01" in body and "OP11 and 8 more" in body
    assert body.index("🔴 **OP00**") < body.index("🟢 **AAA**")


def test_detail_status_colors_and_escaping():
    assert coverage_comment.counts({}) == "—"
    assert coverage_comment.counts({"native": 1000, "opaque": 2}) == (
        "🟢 native: **1,000**<br>🔴 opaque: **2**")
    assert coverage_comment.counts({"partially native": 1}) == "🟡 partially native: **1**"
    assert coverage_comment.counts({"primitive": 1}) == "🟡 primitive: **1**"
    assert coverage_comment.counts({"reference": 1, "ptxas": 2, "none": 3}) == (
        "⚪ none: **3**<br>🟤 ptxas: **2**<br>🟢 reference: **1**")
    assert coverage_comment.counts({"<unknown>|": 1}) == "⚪ &lt;unknown&gt;&#124;: **1**"


def test_added_removed_architectures_and_opcodes():
    before = summary()
    after = summary()
    after["SM100"] = after.pop("SM89")
    report = render(before, after)
    assert "SM89 | 1/2 (50%) | absent | removed" in report["body"]
    assert "SM100 | absent | 1/2 (50%) | added" in report["body"]
    assert "**FADD (added)**" in report["body"]
    assert "**FADD (removed)**" in report["body"]
    assert report["body"].index("| SM89") < report["body"].index("| SM100")


def test_comment_truncation_preserves_full_report():
    after = {"SM89": {"opcodes": {f"OP{i:04}": {"native": 1} for i in range(3000)}}}
    report = render({}, after)
    assert len(report["body"].encode()) <= coverage_comment.COMMENT_LIMIT
    assert "Details truncated" in report["body"]
    assert "coverage-comment-report" in report["body"]
    assert "OP2999" in report["full"]
    assert report["body"].count("<details>") == report["body"].count("</details>")


class GitHub:
    def __init__(self, pages=(), head="head", base="base", state="open", labels=()):
        self.pages = pages
        self.pr = {"state": state, "head": {"sha": head}, "base": {"sha": base},
                   "labels": [{"name": n} for n in labels]}
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET":
            if "/pulls/" in path:
                return self.pr
            page = int(path.rsplit("=", 1)[1])
            return self.pages[page - 1] if page <= len(self.pages) else []
        return {}


def comment(body=coverage_comment.MARKER, user="github-actions[bot]"):
    return {"id": 42, "body": body, "user": {"login": user}}


def publish(report, github):
    coverage_comment.publish(report, "owner/repo", 7, "base", "head", request=github)
    return [call for call in github.calls if call[0] != "GET"]


def test_create_and_no_change_silence():
    report = render({}, summary())
    calls = publish(report, GitHub())
    assert calls == [("POST", "/repos/owner/repo/issues/7/labels", {"labels": ["coverage"]}),
                     ("POST", "/repos/owner/repo/issues/7/comments", {"body": report["body"]})]
    assert publish(render(summary(), summary()), GitHub()) == []


def test_label_follows_changes():
    changed, unchanged = render({}, summary()), render(summary(), summary())
    assert publish(changed, GitHub(labels=["coverage"]))[0][1].endswith("/comments")
    assert publish(unchanged, GitHub(labels=["coverage"])) == [
        ("DELETE", "/repos/owner/repo/issues/7/labels/coverage", None)]


def test_paginated_update_ignores_human_marker():
    report = render({}, summary())
    github = GitHub(pages=[[comment(user="human")] * 100, [comment()]], labels=["coverage"])
    calls = publish(report, github)
    assert calls[0] == ("POST", "/repos/owner/repo/issues/7/comments", {"body": report["body"]})
    assert calls[1][:2] == ("PATCH", "/repos/owner/repo/issues/comments/42")
    archived = calls[1][2]["body"]
    assert archived.startswith(coverage_comment.OUTDATED_MARKER)
    assert "**Outdated:**" in archived
    assert "<summary>Outdated coverage report</summary>" in archived
    assert "<details open" not in archived
    assert any("page=2" in path for _, path, _ in github.calls)


def test_clear_previous_change_and_identical_body():
    report = render(summary(), summary())
    assert publish(report, GitHub(pages=[[comment()]]))[0][0] == "POST"
    assert publish(report, GitHub(pages=[[comment(report["body"])]])) == []


def test_retry_archives_remaining_old_reports_without_duplicate_post():
    report = render({}, summary())
    old = {**comment("%s\nOld report\n<details>\n<summary>SM89</summary>\n\nData\n</details>" % coverage_comment.MARKER), "id": 41}
    github = GitHub(pages=[[old], [comment(report["body"])]], labels=["coverage"])
    # Put the current report on the next page to exercise full pagination.
    github.pages[0] += [comment(user="human")] * 99
    calls = publish(report, github)
    assert len(calls) == 1
    assert calls[0][:2] == ("PATCH", "/repos/owner/repo/issues/comments/41")
    archived = calls[0][2]["body"]
    assert old["body"].removeprefix(coverage_comment.MARKER).lstrip() in archived
    assert archived.count("<details>") == archived.count("</details>")


def test_already_archived_reports_are_ignored():
    report = render({}, summary())
    github = GitHub(pages=[[comment(coverage_comment.OUTDATED_MARKER), comment(report["body"])]], labels=["coverage"])
    assert publish(report, github) == []


def test_failed_post_leaves_previous_report_visible():
    github = GitHub(pages=[[comment()]], labels=["coverage"])

    def request(method, path, payload=None):
        if method == "POST":
            raise RuntimeError("GitHub unavailable")
        return github(method, path, payload)

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        coverage_comment.publish(render({}, summary()), "owner/repo", 7, "base", "head", request=request)
    assert all(method == "GET" for method, _, _ in github.calls)


@pytest.mark.parametrize("kwargs", [{"head": "newer"}, {"base": "newer"}, {"state": "closed"}])
def test_obsolete_run_is_skipped(kwargs):
    github = GitHub(**kwargs)
    assert publish(render({}, summary()), github) == []
    assert len(github.calls) == 1


@pytest.mark.parametrize("event_name,repo", [("push", "owner/repo"), ("pull_request", "fork/repo")])
def test_ineligible_events_skip_before_git_or_api(tmp_path, monkeypatch, event_name, repo):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"repo": {"full_name": repo}}}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    for command in ("report", "publish"):
        monkeypatch.setattr("sys.argv", ["coverage_comment.py", command, "--directory", str(tmp_path)])
        coverage_comment.main()
    assert not (tmp_path / "report.json").exists()


def test_report_cli_reads_commits_and_writes_artifact(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {
        "head": {"sha": "head", "repo": {"full_name": "owner/repo"}},
        "base": {"sha": "base"}}}))
    for key, value in {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request",
                       "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SERVER_URL": "https://github.com",
                       "GITHUB_RUN_ID": "123"}.items():
        monkeypatch.setenv(key, value)
    commands = []

    def git(args, text):
        commands.append(args)
        return json.dumps(summary() if args[-1].startswith("head:") else {})

    monkeypatch.setattr(subprocess, "check_output", git)
    monkeypatch.setattr("sys.argv", ["coverage_comment.py", "report", "--directory", str(tmp_path)])
    coverage_comment.main()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["changed"]
    assert "https://github.com/owner/repo/actions/runs/123" in report["body"]
    assert (tmp_path / "coverage-changes.md").read_text() == report["full"]
    assert commands == [["git", "show", f"{sha}:{coverage_comment.COVERAGE}"] for sha in ("base", "head")]


def test_local_cli_compares_revision_with_working_tree(monkeypatch, capsys):
    for key in ("GITHUB_EVENT_PATH", "GITHUB_EVENT_NAME", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(key, raising=False)
    commands = []

    def git(args, text):
        commands.append(args)
        return json.dumps({})

    monkeypatch.setattr(subprocess, "check_output", git)
    monkeypatch.setattr("sys.argv", ["coverage_comment.py", "local", "main"])
    coverage_comment.main()
    out = capsys.readouterr().out
    assert commands == [["git", "show", f"main:{coverage_comment.COVERAGE}"]]
    assert "Comparing `main` → `working`" in out and "### Changed evidence" in out
    assert "img.shields.io" not in out and "View CI run" not in out


def test_comment_leads_with_head_pinned_coverage_badge():
    body = render(summary(), summary())["body"]
    assert ("https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fowner%2Frepo%2Fhead"
            "%2Fprocessor%2FSASS%2Fsemantics%2Fbadge.json") in body
    assert "(https://raw.githack.com/owner/repo/head/processor/SASS/semantics/coverage.html)" in body


@pytest.mark.parametrize("field,before,after,expected,ambiguous", [
    ("combination_status", {"native": 9600, "partially native": 3840, "opaque": 152448},
     {"native": 28800, "partially native": 11520, "opaque": 125568},
     [("opaque", "native", 19200), ("opaque", "partially native", 7680)], False),
    ("opcodes", {"native": 1}, {"native": 1, "opaque": 2}, [("absent", "opaque", 2)], False),
    ("opcodes", {"native": 2}, {}, [("native", "absent", 2)], False),
    ("combination_evidence", {"none": 6}, {"reference": 2, "none": 8},
     [("opaque", "reference", 2), ("opaque", "none", 2)], False),
    ("combination_evidence", {"gpu": 5, "reference": 5}, {"ptxas": 4, "none": 6},
     [("gpu", "ptxas", 4), ("gpu", "none", 1), ("reference", "none", 5)], True),
    ("opcodes", {"native": 1}, {"native": 1}, [], False),
])
def test_flows(field, before, after, expected, ambiguous):
    assert coverage_comment.flows(field, before, after) == (expected, ambiguous)


def test_sankey_diagrams():
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["combination_status"]["LDG"] = {"native": 3, "opaque": 5}
    body = render(before, after)["body"]
    assert body.count("```mermaid") == 1
    assert "**Modifier combinations**\n\n```mermaid" in body
    assert "sankey-beta\nopaque (before),native (after),3\n```" in body
    assert "others fit" not in body
    assert "height: 80\n" in body
    assert "```mermaid" not in render(before, copy.deepcopy(before))["body"]


def test_summed_verification_diagram():
    before = summary()
    before["SM90"] = copy.deepcopy(before["SM89"])
    after = copy.deepcopy(before)
    for arch in ("SM89", "SM90"):
        after[arch]["combination_evidence"]["FADD"] = {"gpu": 2, "reference": 1, "none": 5}
    body = render(before, after)["body"]
    verification = body[body.index("### Changed evidence"):body.index("#### By architecture")]
    assert "#### Combination evidence" in verification
    assert (verification.index("#### Class evidence") < verification.index("#### Combination evidence")
            < verification.index("| Architecture |"))
    assert "sankey-beta\nreference (before),gpu (after),2\nnone (before),gpu (after),2\n```" in verification
    assert "summed over 2 architectures" in verification
    assert body.count("```mermaid") == 3


def added_semantics(evidence=None):
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["opcodes"]["LDG"] = {"native": 1}
    after["SM89"]["combination_status"]["LDG"] = {"native": 8}
    after["SM89"]["opcode_evidence"]["LDG"] = {"reference": 1} if evidence else {"none": 1}
    after["SM89"]["combination_evidence"]["LDG"] = evidence or {"none": 8}
    return before, after


def test_new_semantics_without_evidence_are_an_improvement_with_a_gap():
    body = render(*added_semantics())["body"]
    assert "**Semantics grew with gaps in recorded evidence**" in body
    assert "classes without evidence **+1**, combinations without evidence **+8** · 1 opcode: LDG" in body
    assert "**LDG** · 🟡 evidence gap grew" in body
    assert "🟢 **LDG**" in body.split("### Semantic coverage changes", 1)[1]
    assert "· 🟡 1 with evidence gaps</summary>" in body
    assert "· 🟢 1 improved</summary>" in body
    assert "⚪ none **+8**" in body
    assert "🔴 none" not in body and "regressed" not in body
    assert "[!CAUTION]" not in body
    assert "does not summarize current test results" in body
    assert "| Class evidence |" in body and "| Combination evidence |" in body
    assert "⚪ No evidence" in body


def test_more_combinations_without_evidence_preserve_existing_checks():
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["combination_status"]["FADD"] = {"native": 16}
    after["SM89"]["combination_evidence"]["FADD"] = {"reference": 2, "none": 14}
    body = render(before, after)["body"]
    assert "**SM89**: combinations without evidence **+8** · 1 opcode: FADD" in body
    assert "**FADD** · 🟡 evidence gap grew" in body
    assert "🟢 **FADD**" in body.split("### Semantic coverage changes", 1)[1]
    assert "regressed" not in body and "[!CAUTION]" not in body
    assert "<summary>Opcode coverage: ⚪ unchanged</summary>" in body
    assert "| SM89 | — | — | — | ⚪ combinations **+8** |" in body


def test_new_semantics_with_partial_evidence_report_only_the_gap():
    body = render(*added_semantics({"reference": 3, "none": 5}))["body"]
    assert "**SM89**: combinations without evidence **+5** · 1 opcode: LDG" in body
    assert "classes without evidence" not in body
    assert "⚪ none **+5**" in body
    assert "regressed" not in body


def test_new_semantics_with_complete_evidence_do_not_warn():
    body = render(*added_semantics({"reference": 8}))["body"]
    assert "**Semantics grew with gaps" not in body
    assert "🟡 evidence gap grew" not in body
    assert "[!WARNING]" not in body and "[!CAUTION]" not in body
    assert "🟢 **LDG**" in body


def test_new_semantics_and_lost_checks_report_both():
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["combination_status"]["FADD"] = {"native": 12}
    after["SM89"]["opcode_evidence"]["FADD"] = {"none": 1}
    after["SM89"]["combination_evidence"]["FADD"] = {"none": 12}
    body = render(before, after)["body"]
    assert "**Reference or stronger evidence decreased**" in body
    assert "**Semantics grew with gaps in recorded evidence**" in body
    assert "**SM89**: combinations without evidence **+6** · 1 opcode: FADD" in body
    assert "🔴 **FADD** · 🟡 evidence gap grew" in body
    assert "🔴 none **+6**" in body
    assert "1 regressed" in body


def test_removing_unevidenced_semantics_does_not_claim_new_checks():
    after, before = added_semantics()
    body = render(before, after)["body"]
    assert "**Modeled support counts decreased**" in body
    assert "⚪ none **-8**" in body
    assert "🟢 none" not in body
    assert "**Semantics grew with gaps" not in body
    assert "[!CAUTION]" not in body


@pytest.mark.parametrize("before,after,expected", [
    ({}, {"none": 8}, 0),
    ({"none": 8}, {}, 0),
    ({"reference": 2, "none": 6}, {"reference": 2, "none": 10}, 0),
    ({"reference": 2, "none": 10}, {"reference": 2, "none": 6}, 0),
    ({"reference": 2, "none": 6}, {"reference": 4, "none": 10}, 0),
    ({"reference": 2, "none": 6}, {"none": 8}, -1),
    ({"reference": 2, "none": 6}, {"none": 12}, -1),
    ({"none": 8}, {"reference": 2, "none": 6}, 1),
])
def test_no_evidence_trend_accounts_for_the_modeled_scope(before, after, expected):
    assert coverage_comment.trend(coverage_comment.EVIDENCE_ORDER, before, after, "none") == expected


def test_gap_warning_survives_comment_truncation_and_bounds_opcode_names(monkeypatch):
    before, after = added_semantics()
    after["SM89"]["opcodes"].update({f"OP{i:02}": {"native": 1} for i in range(20)})
    after["SM89"]["opcode_evidence"].update({f"OP{i:02}": {"none": 1} for i in range(20)})
    monkeypatch.setattr(coverage_comment, "COMMENT_LIMIT", 6_000)
    report = render(before, after)
    assert "**Semantics grew with gaps in recorded evidence**" in report["body"]
    assert "and 9 more" in report["body"]
    assert "Details truncated" in report["body"]
    assert len(report["body"].encode()) <= coverage_comment.COMMENT_LIMIT
