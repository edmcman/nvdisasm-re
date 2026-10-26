"""CI coverage comparisons and sticky comments, without GitHub writes."""
import base64
import copy
import gzip
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


def section(body, title):
    start = body.index(f"### {title}")
    return body[start:body.find("\n### ", start + 1)]


def test_unchanged_and_readable_duplicates():
    base = summary()
    head = copy.deepcopy(base)
    head["SM89_readable"] = {"opcodes": {"OTHER": {"native": 1}}}
    report = render(base, head)
    assert not report["changed"]
    assert "No semantic coverage changes" in report["body"]


def test_report_answers_each_question_in_order():
    body = render(*added_semantics())["body"]
    titles = ["How to read this report", "### Summary", "### Implemented semantics", "### Recorded evidence", "### Evidence gaps",
              "### Needs attention", "### Per-architecture details"]
    assert [body.index(t) for t in titles] == sorted(body.index(t) for t in titles)
    for label in ("What changed", "Implemented semantics", "Recorded evidence", "Evidence gaps", "Needs attention"):
        assert f"- **{label}:**" in section(body, "Summary")
    assert "not this run's test results" in body


@pytest.mark.parametrize("reverse", [False, True])
def test_coverage_gain_and_regression(reverse):
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["opcodes"]["LDG"] = {"partially native": 1}
    if reverse:
        before, after = after, before
    body = render(before, after)["body"]
    assert "pp" not in body and "Opcode coverage" not in body
    assert "- **What changed:** 1 opcode on 1 architecture: LDG (SM89)." in body
    assert ("| LDG | SM89 | 🔴 -1 | — |" if reverse else "| LDG | SM89 | 🟢 +1 | — |") in body
    if reverse:
        assert "🔴 1 combination lost or weakened semantics in LDG (SM89)" not in body  # classes only
        assert "1. 🔴 **LDG** on SM89: 1 class lost or weakened semantics." in body
    else:
        assert "🟢 0 modifier combinations (1 class) gained semantics" in body
        assert "Nothing needs attention." in body
    assert "**FADD**" not in body


def test_evidence_only_change():
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["combination_evidence"]["FADD"] = {"gpu": 2, "none": 6}
    body = render(before, after)["body"]
    assert "⚪ no change in semantics" in body
    assert "🟢 combinations newly recorded with 2 GPU checks; no recorded evidence was lost." in body
    assert "| FADD | SM89 | 🟢 +2 | — | ⚪ -2 | — | — |" in body
    assert "⚪ no change in semantics" in body


def gpu_demoted():
    before = summary()
    before["SM89"]["opcode_evidence"]["FADD"] = {"gpu": 1}
    before["SM89"]["combination_evidence"]["FADD"] = {"gpu": 2, "none": 6}
    return before, summary()


def test_lost_gpu_checks_are_a_gap_with_a_next_check_not_a_failure():
    body = render(*gpu_demoted())["body"]
    assert "🔴 2 combinations lost GPU checks or all evidence in FADD (SM89)" in body
    # The demotion to reference is neutral for reference: nothing became less verified than reference.
    assert "| FADD | SM89 | 🔴 -2 (-1 class) | — | ⚪ +2 (+1 class) | — | — |" in body
    gaps = section(body, "Evidence gaps")
    assert "not test failures" in gaps and "- 🔴 FADD (SM89): 2 combinations lost GPU checks or all evidence" in gaps
    item = section(body, "Needs attention")
    assert "1. 🔴 **FADD** on SM89: 2 combinations no longer have GPU checks." in item
    assert "`nice uv run pytest -m gpu --require-gpu --family <family>` on SM89" in item
    assert "[!CAUTION]" not in body and "fail" not in body.replace("not test failures", "")


def test_gpu_evidence_gain_is_green():
    after, before = gpu_demoted()
    body = render(before, after)["body"]
    assert "| FADD | SM89 | 🟢 +2 (+1 class) | — | ⚪ -2 (-1 class) | — | — |" in body
    assert "no recorded evidence was lost" in body
    assert "Nothing needs attention." in body


def test_lost_evidence_names_the_strongest_level_once():
    before, after = gpu_demoted()
    after["SM89"]["opcode_evidence"]["FADD"] = {"none": 1}
    after["SM89"]["combination_evidence"]["FADD"] = {"none": 8}
    item = section(render(before, after)["body"], "Needs attention")
    assert item.count("**FADD**") == 1 and "no longer have GPU checks" in item


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
    assert "🟢 8 modifier combinations (1 class) gained semantics" in body
    assert "🟡 8 more modeled combinations without recorded evidence" in body
    assert "| LDG | SM89 | — | — | — | 🟡 +8 (+1 class) | — |" in body
    assert "- 🟡 LDG (SM89): 8 more modeled combinations without recorded evidence" in body
    item = section(body, "Needs attention")
    assert "1. 🟡 **LDG** on SM89: 1 class with semantics but no recorded evidence (form names unavailable)." in item
    assert "on an SM89 GPU" in item
    assert "🔴" not in item


def test_new_semantics_with_complete_evidence_do_not_warn():
    body = render(*added_semantics({"reference": 8}))["body"]
    assert "Evidence gaps:** none added." in body
    assert "No evidence was lost and no semantics were added without evidence." in body
    assert "Nothing needs attention." in body


def test_new_semantics_and_lost_checks_report_both():
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["combination_status"]["FADD"] = {"native": 12}
    after["SM89"]["opcode_evidence"]["FADD"] = {"none": 1}
    after["SM89"]["combination_evidence"]["FADD"] = {"none": 12}
    body = render(before, after)["body"]
    assert "🟢 4 modifier combinations gained semantics" in body
    assert "| FADD | SM89 | — | — | 🔴 -2 (-1 class) | 🔴 +6 (+1 class) | — |" in body
    item = section(body, "Needs attention")
    assert "🔴 **FADD** on SM89: 2 combinations lost all recorded evidence." in item
    assert "🟡 **" not in item  # no class was added, so counts alone name no unchecked semantics


def test_removing_unevidenced_semantics_does_not_claim_new_checks():
    after, before = added_semantics()
    body = render(before, after)["body"]
    assert "🔴 8 combinations lost or weakened semantics in LDG (SM89)" in body
    assert "| LDG | SM89 | — | — | — | ⚪ -8 (-1 class) | — |" in body
    assert "newly recorded" not in body


def entry(cls, combos, values=(), kinds=()):
    status = "partially native" if "partially native" in combos else "native" if "native" in combos else "opaque"
    return {"cls": cls, "status": status, "kinds": list(kinds), "combinations": combos, "values": [list(v) for v in values]}


def value(modifier, label, combos, kinds=(), checked=None):
    return (modifier, label, "native", list(kinds), combos, checked or {})


def rendered(before, after, base_detail, head_detail):
    return coverage_comment.render(before, after, "base", "head", "https://example.com/run", "owner/repo",
                                   base_detail, head_detail, {"FADD": "fp32"})["body"]


def test_detail_names_forms_and_the_next_check():
    before, after = summary(), summary()
    after["SM89"]["opcodes"]["FADD"] = {"native": 2}
    after["SM89"]["combination_status"]["FADD"] = {"native": 12}
    after["SM89"]["opcode_evidence"]["FADD"] = {"gpu": 1, "none": 1}
    after["SM89"]["combination_evidence"]["FADD"] = {"gpu": 1, "reference": 1, "none": 10}
    checked = value("ftz", "FTZ", {"native": 4}, ["reference"], {"reference": 1})
    old = entry("fadd__RR", {"native": 8}, [checked, value("Ra@negate", "unset", {"native": 8}, ["reference"], {"reference": 2})], ["reference"])
    new = entry("fadd__RR", {"native": 8}, [value("ftz", "FTZ", {"native": 4}, ["gpu", "reference"], {"gpu": 1}),
                                            value("Ra@negate", "unset", {"native": 8}, ["reference"], {"reference": 2})], ["gpu", "reference"])
    added = entry("fadd__RC", {"native": 4}, [value("rnd", "RZ", {"native": 4}), value("Rb@negate", "set", {"native": 4})])
    body = rendered(before, after, {"SM89": {"FADD": [old]}}, {"SM89": {"FADD": [new, added]}})
    assert "| FADD | SM89 | 🟢 +1 | 🟢 +4 | `Rb@negate`, `rnd=RZ` | — |" in body
    # A form is listed under the strongest kind it gained; unset flags (the default) are not forms.
    assert "| 🔵 `ftz=FTZ` |" in body and "🟢 `ftz=FTZ`" not in body and "unset" not in body
    item = section(body, "Needs attention")
    assert ("1. 🟡 **FADD** on SM89: semantics without any recorded evidence: `fadd__RC`, `Rb@negate`, `rnd=RZ`.<br>Next: add emulator "
            "cases for these forms to `tests/cases/fp32.py` from a reference in `tests/refs/`, then run "
            "`nice uv run pytest -m gpu --require-gpu --family fp32` on an SM89 GPU.") in item


def test_weakened_class_is_named_when_opcode_gains_hide_it():
    before = {"SM75": {"opcodes": {"FADD": {"native": 1, "opaque": 1}}, "combination_status": {"FADD": {"native": 8, "opaque": 8}}}}
    after = {"SM75": {"opcodes": {"FADD": {"native": 1, "partially native": 1}},
                      "combination_status": {"FADD": {"native": 14, "partially native": 2}}}}
    old = [entry("fadd__RR", {"native": 8}), entry("fadd__RC", {"opaque": 8})]
    new = [entry("fadd__RR", {"native": 6, "partially native": 2}), entry("fadd__RC", {"native": 8})]
    body = rendered(before, after, {"SM75": {"FADD": old}}, {"SM75": {"FADD": new}})
    assert "| FADD | SM75 | 🟢 +1 | 🟢 +8<br>🔴 2 weakened | — | `fadd__RR` |" in body
    assert "🔴 2 combinations lost or weakened semantics in FADD (SM75)" in body
    item = section(body, "Needs attention")
    assert "1. 🔴 **FADD** on SM75: 2 combinations lost or weakened semantics: `fadd__RR`." in item
    assert "--family fp32 --arch SM75" in item
    assert "2. 🟡 **FADD** on SM75: semantics without any recorded evidence: `fadd__RC`." in item
    assert "no GPU is available for this architecture" in item


def test_forms_checked_in_fewer_combinations_are_named():
    before, after = gpu_demoted()
    old = entry("fadd__RR", {"native": 8}, [value("ftz", "FTZ", {"native": 4}, ["gpu"], {"gpu": 2})], ["gpu"])
    new = entry("fadd__RR", {"native": 8}, [value("ftz", "FTZ", {"native": 4}, ["gpu"], {"gpu": 1})], ["gpu"])
    item = section(rendered(before, after, {"SM89": {"FADD": [old]}}, {"SM89": {"FADD": [new]}}), "Needs attention")
    assert "**FADD** on SM89: 2 combinations no longer have GPU checks: `ftz=FTZ`." in item


def test_identical_items_merge_architectures():
    before = {a: summary()["SM89"] for a in ("SM100", "SM101", "SM103", "SM89")}
    after = {a: added_semantics()[1]["SM89"] for a in before}
    body = render(before, after)["body"]
    assert "LDG (SM89–SM103)" in body
    assert "| LDG | SM89–SM103 | 🟢 +1 | 🟢 +8 | — | — |" in body
    assert "🟡 **LDG** on SM100–SM103:" in body and "--arch SM100`; no GPU" in body
    assert "🟡 **LDG** on SM89:" in body


def test_arch_list():
    order = ["SM75", "SM80", "SM86", "SM89", "SM90", "SM100"]
    assert coverage_comment.arch_list(["SM80", "SM75"], order) == "SM75, SM80"
    assert coverage_comment.arch_list(["SM75", "SM80", "SM86", "SM100"], order) == "SM75–SM86, SM100"


def test_detail_status_colors_and_escaping():
    assert coverage_comment.counts({}) == "—"
    assert coverage_comment.counts({"native": 1000, "opaque": 2}) == (
        "🟢 native: **1,000**<br>🔴 opaque: **2**")
    assert coverage_comment.counts({"partially native": 1}) == "🟡 partially native: **1**"
    assert coverage_comment.counts({"reference": 1, "ptxas": 2, "none": 3}) == (
        "⚪ none: **3**<br>🟤 ptxas: **2**<br>🟢 reference: **1**")
    assert coverage_comment.counts({"<unknown>|": 1}) == "⚪ &lt;unknown&gt;&#124;: **1**"


def test_added_removed_architectures_and_opcodes():
    before = summary()
    after = summary()
    after["SM100"] = after.pop("SM89")
    body = render(before, after)["body"]
    assert "<summary>SM89 (removed) · 2 changed opcodes</summary>" in body
    assert "<summary>SM100 (added) · 2 changed opcodes</summary>" in body
    assert "**FADD (added)**" in body and "**FADD (removed)**" in body
    assert body.index("<summary>SM89") < body.index("<summary>SM100")


def test_comment_truncation_preserves_full_report():
    after = {"SM89": {"opcodes": {f"OP{i:04}": {"native": 1} for i in range(3000)}}}
    report = render({}, after)
    assert len(report["body"].encode()) <= coverage_comment.COMMENT_LIMIT
    assert "Details truncated" in report["body"]
    assert "more rows in the full report (`coverage-comment-report`)" in report["body"]
    assert "OP2999" in report["full"] and "OP2999" not in report["body"]
    assert report["body"].count("<details>") == report["body"].count("</details>")


def test_sankey_diagrams_in_architecture_details():
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["combination_status"]["LDG"] = {"native": 3, "opaque": 5}
    body = render(before, after)["body"]
    assert body.count("```mermaid") == 1
    assert body.index("```mermaid") > body.index("### Per-architecture details")
    assert "sankey-beta\nopaque (before),native (after),3\n```" in body
    assert "height: 80\n" in body
    assert "```mermaid" not in render(before, copy.deepcopy(before))["body"]


@pytest.mark.parametrize("field,before,after,expected,ambiguous", [
    ("combination_status", {"native": 9600, "partially native": 3840, "opaque": 152448},
     {"native": 28800, "partially native": 11520, "opaque": 125568},
     [("opaque", "native", 19200), ("opaque", "partially native", 7680)], False),
    ("opcodes", {"native": 1}, {"native": 1, "opaque": 2}, [("absent", "opaque", 2)], False),
    ("opcodes", {"native": 2}, {}, [("native", "absent", 2)], False),
    ("combination_evidence", {"none": 6}, {"reference": 2, "none": 8},
     [("no semantics", "reference", 2), ("no semantics", "none", 2)], False),
    ("combination_evidence", {"gpu": 5, "reference": 5}, {"ptxas": 4, "none": 6},
     [("gpu", "ptxas", 4), ("gpu", "none", 1), ("reference", "none", 5)], True),
    ("opcodes", {"native": 1}, {"native": 1}, [], False),
])
def test_flows(field, before, after, expected, ambiguous):
    assert coverage_comment.flows(field, before, after) == (expected, ambiguous)


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


def test_comment_links_the_head_coverage_report():
    body = render(summary(), summary())["body"]
    assert "[Full coverage report](https://raw.githack.com/owner/repo/head/processor/SASS/semantics/coverage.html)" in body
    assert "img.shields.io" not in body


def page(detail):
    """A coverage page embedding `detail` ({arch: {opcode: [entry]}}) as sass.sleigh.coverage.report does."""
    classes = [e for ops in detail.values() for entries in ops.values() for e in entries]
    members = {arch: {op: [classes.index(e) for e in entries] for op, entries in ops.items()} for arch, ops in detail.items()}
    data = base64.b64encode(gzip.compress(json.dumps({"classes": classes, "members": members}).encode())).decode()
    return f'<script>const DATA="{data}";</script>'


def test_unpack_reads_the_coverage_page():
    detail = {"SM89": {"FADD": [entry("fadd__RR", {"native": 8})]}}
    assert coverage_comment.unpack(page(detail)) == detail


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
    for key, setting in {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request",
                         "GITHUB_REPOSITORY": "owner/repo", "GITHUB_SERVER_URL": "https://github.com",
                         "GITHUB_RUN_ID": "123"}.items():
        monkeypatch.setenv(key, setting)
    detail = {"SM89": {"FADD": [entry("fadd__RR", {"native": 8}, [value("ftz", "FTZ", {"native": 8})])]}}
    files = {f"base:{coverage_comment.COVERAGE}": json.dumps({}), f"head:{coverage_comment.COVERAGE}": json.dumps(summary()),
             f"base:{coverage_comment.PAGE}": page({}), f"head:{coverage_comment.PAGE}": page(detail),
             f"head:{coverage_comment.EVIDENCE}": json.dumps({"SM89": {"fadd__RR": {"gpu": ["cases/fp32"]}}})}
    commands = []

    def git(args, text):
        commands.append(args[-1])
        return files[args[-1]]

    monkeypatch.setattr(subprocess, "check_output", git)
    monkeypatch.setattr("sys.argv", ["coverage_comment.py", "report", "--directory", str(tmp_path)])
    coverage_comment.main()
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["changed"]
    assert "https://github.com/owner/repo/actions/runs/123" in report["body"]
    assert "`ftz=FTZ`" in report["body"] and "--family fp32" in report["body"]
    assert (tmp_path / "coverage-changes.md").read_text() == report["full"]
    assert sorted(commands) == sorted(files)


def test_missing_pages_fall_back_to_counts(monkeypatch):
    def git(args, text):
        if args[-1].endswith("coverage.json"):
            return json.dumps(summary() if args[-1].startswith("head:") else {})
        raise subprocess.CalledProcessError(128, args)

    monkeypatch.setattr(subprocess, "check_output", git)
    body = coverage_comment.compare("base", "head")["body"]
    assert "Form names are unavailable" in body and "<family>" not in section(body, "Summary")


def test_local_cli_compares_revision_with_working_tree(monkeypatch, capsys):
    for key in ("GITHUB_EVENT_PATH", "GITHUB_EVENT_NAME", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(key, raising=False)
    commands = []

    def git(args, text):
        commands.append(args[-1])
        return json.dumps({}) if args[-1].endswith("coverage.json") else page({})

    monkeypatch.setattr(subprocess, "check_output", git)
    monkeypatch.setattr("sys.argv", ["coverage_comment.py", "local", "main"])
    coverage_comment.main()
    out = capsys.readouterr().out
    assert sorted(commands) == [f"main:{coverage_comment.PAGE}", f"main:{coverage_comment.COVERAGE}"]
    assert "Comparing `main` → `working`" in out and "### Per-architecture details" in out
    assert "Full coverage report" not in out and "View CI run" not in out
