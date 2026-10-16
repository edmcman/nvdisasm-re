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
    return coverage_comment.render(before, after, "base", "head", "https://example.com/run")


def test_unchanged_and_readable_duplicates():
    base = summary()
    head = copy.deepcopy(base)
    head["SM89_readable"] = {"opcodes": {"OTHER": {"native": 1}}}
    report = render(base, head)
    assert not report["changed"]
    assert "No semantic coverage changes" in report["body"]


@pytest.mark.parametrize("reverse", [False, True])
def test_coverage_gain_and_regression(reverse):
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"]["opcodes"]["LDG"] = {"partially native": 1}
    if reverse:
        before, after = after, before
    report = render(before, after)
    assert report["changed"]
    assert ("-50 pp" if reverse else "+50 pp") in report["body"]
    assert "1/2 (50%)" in report["body"]
    assert "2/2 (100%)" in report["body"]
    assert "**LDG**" in report["body"]
    assert "**FADD**" not in report["body"]


@pytest.mark.parametrize("field,label", coverage_comment.FIELDS[1:])
def test_combination_and_verification_only_changes(field, label):
    before = summary()
    after = copy.deepcopy(before)
    after["SM89"][field]["FADD"] = {"gpu": 3}
    report = render(before, after)
    assert report["changed"]
    assert "+0 pp" in report["body"]
    assert f"{label}:" in report["body"]
    assert "gpu: 3" in report["body"]


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
    def __init__(self, pages=(), head="head", base="base", state="open"):
        self.pages = pages
        self.pr = {"state": state, "head": {"sha": head}, "base": {"sha": base}}
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
    assert calls == [("POST", "/repos/owner/repo/issues/7/comments", {"body": report["body"]})]
    assert publish(render(summary(), summary()), GitHub()) == []


def test_paginated_update_ignores_human_marker():
    report = render({}, summary())
    github = GitHub(pages=[[comment(user="human")] * 100, [comment()]])
    assert publish(report, github) == [
        ("PATCH", "/repos/owner/repo/issues/comments/42", {"body": report["body"]})]
    assert any("page=2" in path for _, path, _ in github.calls)


def test_clear_previous_change_and_identical_body():
    report = render(summary(), summary())
    assert publish(report, GitHub(pages=[[comment()]]))[0][0] == "PATCH"
    assert publish(report, GitHub(pages=[[comment(report["body"])]])) == []


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
