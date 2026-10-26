"""Compare committed semantic coverage and archive superseded PR comments.

Uses only the standard library; does not import or regenerate SASS semantics.
"""
import argparse
import base64
from collections import Counter
import gzip
import html
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request


MARKER = "<!-- nvdisasm-re:coverage -->"
OUTDATED_MARKER = "<!-- nvdisasm-re:coverage-outdated -->"
LABEL = "coverage"
COVERAGE = "processor/SASS/semantics/coverage.json"
PAGE = "processor/SASS/semantics/coverage.html"  # embeds per-class detail: modifier values, their status and evidence
EVIDENCE = "processor/SASS/semantics/evidence.json"  # names the case family whose GPU checks ran a class
BADGE = "processor/SASS/semantics/badge.json"
ROOT = Path(__file__).resolve().parents[2]
COMMENT_LIMIT = 60_000  # Leave room below GitHub's comment size limit.
GPU_ARCHES = ("SM89", "SM90")  # architectures with recorded GPU runs (docs/hardware-facts.md)
FIELDS = (
    ("opcodes", "Classes"),
    ("combination_status", "Modifier combinations"),
    ("opcode_evidence", "Class evidence"),
    ("combination_evidence", "Combination evidence"),
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
EVIDENCE_LABELS = {"gpu": "GPU checks", "ptxas": "PTX compilation",
                   "reference": "Reference cases", "none": "No evidence"}
# The next check that restores a level of evidence; {family} is the opcode's case family.
RESTORE = {
    "gpu": "re-run `nice uv run pytest -m gpu --require-gpu --family {family}` on {gpu}, then `nice uv run sass-refresh-coverage`; "
           "GPU observations (`tests/baseline/gpu_observations/`) must match the current cases",
    "ptxas": "check that the annotated `tests/ptx` probe still emits these forms",
    "reference": "restore emulator cases for these forms in `tests/cases/{family}.py` "
                 "(`uv run pytest -k \"lifted or family_lowering\" --family {family} --arch {arch}`)",
}
LIMIT_NAMES = 12  # opcodes, classes or forms named in one place
LIMIT_ROWS = 25  # table rows and attention items in a comment that would otherwise exceed COMMENT_LIMIT
# Where counts come from or go when a total changes: classes are added or removed; combinations
# enter or leave the evidence counts by gaining or losing semantics.
SOURCE = {"opcodes": "absent", "combination_status": "absent",
          "opcode_evidence": "no semantics", "combination_evidence": "no semantics"}


def counts(values):
    return "<br>".join(f"{STATUS_COLORS.get(k, '⚪')} {html.escape(k).replace('|', '&#124;')}: **{v:,}**"
                      for k, v in sorted(values.items())) or "—"


def trend(order, before, after, kind):
    """+1/-1/0: whether `kind`'s change is an improvement; the last level (opaque, none) is better lower."""
    if kind not in order:
        return 0
    i = order.index(kind)
    levels = order[i:i + 1] if i == len(order) - 1 else order[:i + 1]
    d = sum(after.get(k, 0) - before.get(k, 0) for k in levels)
    if order == EVIDENCE_ORDER and kind == "none":
        scope = sum(after.values()) - sum(before.values())
        # New modeled forms can have no checks; removed ones take their gaps with them.
        # Neither change alone means existing evidence was lost or checks were added.
        d = max(0, d - max(0, scope)) if d > 0 else min(0, d - min(0, scope))
    if i == len(order) - 1:
        d = -d
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
    # Mermaid's default 600x400 is far taller than a few links need.
    height = 40 + 40 * max(len({a for a, _ in moved}), len({b for _, b in moved}))
    note = "Flows inferred from the counts, assuming the fewest moves" + (
        f"; others fit {names(ambiguous)} equally" if ambiguous else "") + "."
    # Mermaid blends links with an inline `multiply`, which turns them black on GitHub's dark background.
    return (f"\n**{title}**\n\n```mermaid\n---\nconfig:\n"
            '  themeCSS: ".link, .link path { mix-blend-mode: normal !important; stroke-opacity: 0.6; }"\n'
            "  sankey:\n    showValues: true\n    linkColor: target\n"
            f"    width: 800\n    height: {height}\n---\nsankey-beta\n{links}\n```\n\n{note}\n")


def diagrams(old, new, changed):
    """A Sankey diagram per metric of what moved between levels across one architecture's changed opcodes."""
    out = []
    for field, title in FIELDS:
        moved, ambiguous = Counter(), {}
        for op in changed:
            links, unclear = flows(field, old.get(field, {}).get(op, {}), new.get(field, {}).get(op, {}))
            for a, b, n in links:
                moved[a, b] += n
            if unclear:
                ambiguous[op] = None
        if moved:
            rank = {k: i for i, k in enumerate(ranked(ORDERS[field], {k for link in moved for k in link}))}
            moved = dict(sorted(moved.items(), key=lambda item: (rank[item[0][0]], rank[item[0][1]])))
            out.append(sankey(title, moved, list(ambiguous)))
    return out


def at_least(values, order, kind):
    return sum(values.get(k, 0) for k in order[:order.index(kind) + 1])


def modeled(values):
    """Classes or combinations with semantics, of any kind."""
    return sum(n for k, n in values.items() if k not in ("opaque", "absent"))


def weakened(before, after):
    """Combinations (or classes) that lost semantics or fell to a weaker kind of semantics."""
    return max([0] + [at_least(before, STATUS_ORDER, k) - at_least(after, STATUS_ORDER, k) for k in STATUS_ORDER[:-1]])


def lost(before, after, kind):
    """Combinations (or classes) no longer recorded with `kind` or stronger evidence."""
    return max(0, at_least(before, EVIDENCE_ORDER, kind) - at_least(after, EVIDENCE_ORDER, kind))


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


def names(items, code=False):
    shown = ", ".join(f"`{i}`" if code else html.escape(i) for i in items[:LIMIT_NAMES])
    return shown + (f" and {len(items) - LIMIT_NAMES} more" if len(items) > LIMIT_NAMES else "")


def arch_list(archs, order):
    """`SM80, SM86`, with runs of three or more consecutive architectures of `order` as `SM100–SM120`."""
    runs = []
    for i in sorted(order.index(a) for a in archs):
        if runs and i == runs[-1][-1] + 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return ", ".join(f"{order[r[0]]}–{order[r[-1]]}" if len(r) > 2 else ", ".join(order[i] for i in r) for r in runs)


def plural(n, word):
    return f"{n:,} {word}" + ("" if n == 1 else "es" if word.endswith("s") else "s")


def unpack(page):
    """{arch: {opcode: [class entry]}} from the coverage page's embedded data (sass.sleigh.coverage.report)."""
    data = json.loads(gzip.decompress(base64.b64decode(re.search(r'const DATA="([^"]+)"', page).group(1))))
    return {arch: {op: [data["classes"][i] for i in ix] for op, ix in ops.items()} for arch, ops in data["members"].items()}


def form_name(modifier, label):
    """A modifier value as the coverage page writes it: `ftz=FTZ`, or a set flag's name."""
    return modifier if label == "set" and "@" in modifier else f"{modifier}={label}"


def forms(entries):
    """{form: (combinations by status, evidence kinds, checked combinations by strongest kind)} of an
    opcode's classes. A form is one modifier value, summed over the classes that have it; unset flags
    are the default and omitted."""
    out = {}
    for e in entries:
        for modifier, label, status, kinds, combos, checked in e.get("values", []):
            if modifier is None or label == "unset":
                continue
            c, ks, t = out.setdefault(form_name(modifier, label), (Counter(), set(), Counter()))
            c.update(combos or {status: 1})
            ks.update(kinds)
            t.update(checked)
    return out


def form_changes(old, new):
    """{change: [form or class]} between an opcode's class entries before and after: semantics
    gained, lost or weakened; evidence gained (+kind), lost (-kind) or
    had by fewer of the form's combinations (fewer kind); added semantics never run by
    any evidence (unchecked). Class names carry the classes' own changes (gained, lost, weakened,
    unchecked and -kind classes); weakened combinations counts those that the weakened classes lost,
    which an opcode's totals hide when other classes gain."""
    out, empty = {}, (Counter(), set(), Counter())
    fo, fn = forms(old), forms(new)
    for f in sorted(fo.keys() | fn.keys()):
        (cb, kb, tb), (ca, ka, ta) = fo.get(f, empty), fn.get(f, empty)
        if modeled(ca) and not modeled(cb):
            out.setdefault("gained", []).append(f)
        elif modeled(cb) and not modeled(ca):
            out.setdefault("lost", []).append(f)
        elif weakened(cb, ca):
            out.setdefault("weakened", []).append(f)
        if modeled(ca) > modeled(cb) and not ka:
            out.setdefault("unchecked", []).append(f)
        # A form is listed once, under the strongest kind it gained or lost.
        for sign, kinds in (("+", ka - kb), ("-", kb - ka)):
            if strongest := next((k for k in EVIDENCE_ORDER if k in kinds), None):
                out.setdefault(sign + strongest, []).append(f)
        # Or still checked, but in fewer combinations.
        if strongest := next((k for k in EVIDENCE_ORDER[:-1] if lost(tb, ta, k)), None):
            if f not in out.get("-" + strongest, []):
                out.setdefault("fewer " + strongest, []).append(f)
    co, cn = {e["cls"]: e for e in old}, {e["cls"]: e for e in new}
    for c in sorted(co.keys() | cn.keys()):
        b, a = co.get(c, {}), cn.get(c, {})
        sb, sa = b.get("status", "opaque"), a.get("status", "opaque")
        if sb == "opaque" != sa:
            out.setdefault("gained classes", []).append(c)
            if not a.get("kinds"):
                out.setdefault("unchecked classes", []).append(c)
        elif sa == "opaque" != sb:
            out.setdefault("lost classes", []).append(c)
        elif n := weakened(b.get("combinations", {}), a.get("combinations", {})):
            out.setdefault("weakened classes", []).append(c)
            out["weakened combinations"] = out.get("weakened combinations", 0) + n
        if strongest := next((k for k in EVIDENCE_ORDER if k in set(b.get("kinds", [])) - set(a.get("kinds", []))), None):
            out.setdefault(f"-{strongest} classes", []).append(c)
    return out


def fell_names(fc):
    """The classes and forms that lost or weakened semantics; a weakened class stands for its forms,
    which share its combinations."""
    return (fc.get("lost classes", []) + fc.get("weakened classes", []) + fc.get("lost", [])
            + ([] if fc.get("weakened classes") else fc.get("weakened", [])))


def families(evidence, detail):
    """{opcode: case family} from the GPU sources (`cases/F`) recorded for the opcode's classes."""
    out = {}
    for arch, ops in detail.items():
        for op, entries in ops.items():
            for e in entries:
                for source in evidence.get(arch, {}).get(e["cls"], {}).get("gpu", []):
                    if source.startswith("cases/"):
                        out.setdefault(op, Counter())[source.removeprefix("cases/")] += 1
    return {op: c.most_common(1)[0][0] for op, c in out.items()}


HOW_TO_READ = ("<details>\n<summary>How to read this report</summary>\n\n"
               "- A **class** is one encoding of an opcode (operand form); a **modifier combination** is one legal choice of its modifiers; "
               "a **form** is one modifier value (`ftz=FTZ`, or a set flag such as `Ra@negate`) over the opcode's classes.\n"
               "- **Semantics** are native, partially native (some combinations fall back to an opaque call), primitive or opaque (none). "
               "Weakened means fewer combinations at a level or stronger, such as native becoming partially native.\n"
               "- **Recorded evidence** counts each combination once under its strongest kind: 🔵 GPU checks (passing, on the "
               "architecture where they ran: SM89 and SM90), 🟤 PTX compilation (the compiler emits the form; results unchecked), "
               "🟢 reference cases (available cases, not this run's results), ⚪ none. A combination moving between kinds counts as a "
               "gain or loss only for the stronger kind.\n"
               "- 🟢 better · 🔴 worse · 🟡 a gap that comes with added semantics · ⚪ neutral.\n"
               "- **Opcode coverage** counts opcodes with at least one class with semantics, in percentage points (pp).\n"
               "- Per-architecture details give each changed opcode's counts and diagrams of what moved between levels, "
               "inferred from the counts assuming the fewest moves.\n"
               "- Readable variants share the exact variants' coverage and are omitted.\n\n"
               "</details>\n\n### Per-architecture details\n\n")


def badge(repo, sha):
    """The README's opcode-coverage badge, read from the PR head's committed badge.json."""
    raw = urllib.parse.quote(f"https://raw.githubusercontent.com/{repo}/{sha}/{BADGE}", safe="")
    report = f"https://raw.githack.com/{repo}/{sha}/{PAGE}"
    return f"[![opcode semantics](https://img.shields.io/endpoint?url={raw})]({report})"


def grouped(items, order):
    """[(opcode, architectures, value)] of {(arch, opcode): value}, one row per opcode and distinct value."""
    by = {}
    for (arch, op), value in items.items():
        by.setdefault((op, value), []).append(arch)
    return [(op, archs, value) for (op, value), archs in
            sorted(by.items(), key=lambda item: (item[0][0], min(order.index(a) for a in item[1])))]


def signed(d, word=None):
    return f"{d:+,}" + (f" {word}" if word else "")


def evidence_cell(kind, b, a, bc, ac):
    """One recorded-evidence cell: the combinations' change at `kind` (classes in parentheses)."""
    d, dc = a.get(kind, 0) - b.get(kind, 0), ac.get(kind, 0) - bc.get(kind, 0)
    if not d and not dc:
        return "—"
    t = trend(EVIDENCE_ORDER, b, a, kind) if d else trend(EVIDENCE_ORDER, bc, ac, kind)
    # A kind's count can rise while the level judged with the stronger ones falls (GPU demoted to reference).
    mark = "🟡" if kind == "none" and t == 0 and (d > 0 or dc > 0) else "⚪" if kind != "none" and t < 0 < (d or dc) else tone(t)
    return f"{mark} {signed(d)}" + (f" ({signed(dc, 'classes' if abs(dc) != 1 else 'class')})" if dc else "")


def attention(changes, order, family):
    """[(mark, opcode, architectures, what, next check)] for semantics that weakened, evidence that was
    lost (each opcode once, at the strongest level), and semantics added without any evidence."""
    items = {}

    def add(arch, op, mark, what, next_check):
        items.setdefault((op, mark, what, next_check), []).append(arch)

    for (arch, op), (old, new, fc) in changes.items():
        get = lambda side, field: side.get(field, {}).get(op, {})
        n = max(weakened(get(old, "combination_status"), get(new, "combination_status")), fc.get("weakened combinations", 0))
        nc = weakened(get(old, "opcodes"), get(new, "opcodes"))
        shown = fell_names(fc)
        if n or nc or shown:
            what = (f"{plural(n, 'combination')} lost or weakened semantics" if n else f"{plural(nc, 'class')} lost or weakened semantics"
                    if nc else "modifier forms lost or weakened semantics")
            add(arch, op, "🔴", what + (f": {names(shown, code=True)}" if shown else ""),
                "check whether the fallback is intended (the family's selector guards), then run "
                "`uv run pytest -k \"lifted or family_lowering\" --family {family} --arch {arch}`")
        for kind in ("gpu", "ptxas", "reference"):
            n = lost(get(old, "combination_evidence"), get(new, "combination_evidence"), kind)
            nc = lost(get(old, "opcode_evidence"), get(new, "opcode_evidence"), kind)
            shown = fc.get(f"-{kind} classes", []) + fc.get(f"-{kind}", []) + fc.get(f"fewer {kind}", [])
            if n or nc or shown:
                label = {"gpu": "no longer have GPU checks", "ptxas": "no longer have PTX compilation or GPU checks",
                         "reference": "lost all recorded evidence"}[kind]
                what = f"{plural(n, 'combination') if n else plural(nc, 'class') if nc else 'modifier forms'} {label}"
                add(arch, op, "🔴", what + (f": {names(shown, code=True)}" if shown else ""), RESTORE[kind])
                break
        shown = fc.get("unchecked classes", []) + fc.get("unchecked", [])
        # Without form detail, only added classes without evidence are certain gaps.
        n = 0 if fc else max(0, min(get(new, "opcode_evidence").get("none", 0) - get(old, "opcode_evidence").get("none", 0),
                                    modeled(get(new, "opcodes")) - modeled(get(old, "opcodes"))))
        if shown or n:
            what = (f"semantics without any recorded evidence: {names(shown, code=True)}" if shown
                    else f"{plural(n, 'class')} with semantics but no recorded evidence (form names unavailable)")
            next_check = ("add emulator cases for these forms to `tests/cases/{family}.py` from a reference in `tests/refs/`, "
                          "then run `nice uv run pytest -m gpu --require-gpu --family {family}` on an {gpu} GPU"
                          if arch in GPU_ARCHES else
                          "add reference emulator cases for these forms to `tests/cases/{family}.py` and run "
                          "`uv run pytest -k \"lifted or family_lowering\" --family {family} --arch {arch}`; "
                          "no GPU is available for this architecture")
            add(arch, op, "🟡", what, next_check)
    out = []
    for (op, mark, what, next_check), archs in items.items():
        archs = sorted(archs, key=order.index)
        filled = (next_check.replace("{family}", family.get(op, "<family>")).replace("{arch}", archs[0])
                  .replace("{gpu}", " or ".join(a for a in archs if a in GPU_ARCHES) or "SM89 or SM90"))
        out.append((mark, op, arch_list(archs, order), what, filled, order.index(archs[0])))
    return [item[:5] for item in sorted(out, key=lambda item: (item[0] != "🔴", item[1], item[5]))]


def render(base, head, base_sha, head_sha, run_url=None, repo=None, base_detail=None, head_detail=None, family=None):
    """Return the full report, bounded comment, and whether reportable data changed. `base_detail` and
    `head_detail` are the coverage pages' per-class detail (`unpack`), which names the changed forms;
    without both, the report gives counts only. `family` maps opcodes to case families for the suggested
    commands. Without a repository or run (a local comparison) the badge and CI-run link are omitted."""
    family = family or {}
    detailed = base_detail is not None and head_detail is not None
    order = sorted((a for a in base.keys() | head.keys() if "_" not in a), key=lambda a: int(a.removeprefix("SM")))
    rows, sections, changes, moved = [], [], {}, False
    for arch in order:
        before, after = base.get(arch), head.get(arch)
        old, new = before or {}, after or {}
        ops = sorted(old.get("opcodes", {}).keys() | new.get("opcodes", {}).keys())
        fc = {op: form_changes(base_detail.get(arch, {}).get(op, []), head_detail.get(arch, {}).get(op, []))
              for op in ops} if detailed else {}
        changed = [op for op in ops if fc.get(op) or any(
            old.get(field, {}).get(op) != new.get(field, {}).get(op) for field, _ in FIELDS)]
        if before is not None and after is not None and not changed:
            continue
        changes.update({(arch, op): (old, new, fc.get(op, {})) for op in changed})
        if before is not None and after is not None:
            pp = opcode_totals(after)[2] - opcode_totals(before)[2]
            delta = f"{tone(pp)} {pp:+d} pp"
            moved |= pp != 0
        else:
            delta = "added" if after is not None else "removed"
            moved = True
        rows.append(f"| {arch} | {totals_text(before)} | {totals_text(after)} | {delta} |")
        details = [f"<details>\n<summary>{arch} · {plural(len(changed), 'changed opcode')}</summary>\n", *diagrams(old, new, changed)]
        for op in changed:
            label = " (added)" if op not in old.get("opcodes", {}) else " (removed)" if op not in new.get("opcodes", {}) else ""
            details.append(f"\n**{html.escape(op)}{label}**\n")
            details.extend(["| Metric | Before | After | Change |", "| :--- | ---: | ---: | :--- |"])
            for field, title in FIELDS:
                b, h = old.get(field, {}).get(op, {}), new.get(field, {}).get(op, {})
                if b != h:
                    details.append(f"| {title} | {counts(b)} | {counts(h)} | {change(field, b, h)} |")
        details.append("\n</details>\n")
        sections.append("\n".join(details))
    header = (f"{MARKER}\n## 🧪 Semantic coverage\n\n" + (f"{badge(repo, head_sha)}\n\n" if repo else "")
              + f"Comparing `{base_sha[:7]}` → `{head_sha[:7]}`" + (f" · [View CI run]({run_url})" if run_url else "") + "\n\n"
              + "This report compares the committed coverage files. Implemented semantics and recorded evidence are separate; "
              "evidence means recorded GPU checks, PTX compilation or reference cases, not this run's test results, "
              "which CI's test jobs report.\n\n")
    if not rows:
        body = header + "No semantic coverage changes compared with the PR base.\n"
        return {"full": body, "body": body, "changed": False}

    def get(key, side, field):
        return changes[key][side].get(field, {}).get(key[1], {})

    # Semantics: classes and combinations with semantics, and the modifier forms that gained or lost them.
    semantics = {}
    for key, (_, _, fc) in changes.items():
        cells = []
        for field in ("opcodes", "combination_status"):
            b, a = get(key, 0, field), get(key, 1, field)
            d, w = modeled(a) - modeled(b), max(weakened(b, a), fc.get("weakened combinations", 0) if field != "opcodes" else 0)
            parts = ([f"{tone(d)} {signed(d)}"] if d else []) + ([f"🔴 {w:,} weakened"] if w and w != -d else [])
            cells.append("<br>".join(parts) or "—")
        gained = fc.get("gained", [])
        cells += [names(gained, code=True) or "—", names(fell_names(fc), code=True) or "—"]
        if cells != ["—"] * 4:
            semantics[key] = tuple(cells)
    # Evidence: combinations (classes) by strongest recorded evidence, and the forms that gained it.
    evidence = {}
    for key, (_, _, fc) in changes.items():
        b, a = get(key, 0, "combination_evidence"), get(key, 1, "combination_evidence")
        bc, ac = get(key, 0, "opcode_evidence"), get(key, 1, "opcode_evidence")
        cells = [evidence_cell(k, b, a, bc, ac) for k in EVIDENCE_ORDER]
        checked = [f"{STATUS_COLORS[k]} {names(fc['+' + k], code=True)}" for k in ("gpu", "ptxas", "reference") if fc.get("+" + k)]
        cells.append("<br>".join(checked) or "—")
        if cells != ["—"] * 5:
            evidence[key] = tuple(cells)
    items = attention(changes, order, family)

    # Summary: one line per question.
    by_op = {}
    for arch, op in changes:
        by_op.setdefault(op, []).append(arch)
    total = lambda f: sum(f(key) for key in changes)
    gained = total(lambda k: max(0, modeled(get(k, 1, "combination_status")) - modeled(get(k, 0, "combination_status"))))
    gained_classes = total(lambda k: max(0, modeled(get(k, 1, "opcodes")) - modeled(get(k, 0, "opcodes"))))
    fell = {k: max(weakened(get(k, 0, "combination_status"), get(k, 1, "combination_status")),
                   changes[k][2].get("weakened combinations", 0)) for k in changes}
    fell = {k: n for k, n in fell.items() if n}
    checks = {k: total(lambda key: max(0, get(key, 1, "combination_evidence").get(k, 0) - get(key, 0, "combination_evidence").get(k, 0)))
              for k in ("gpu", "ptxas", "reference")}
    dropped = {k: lost(get(k, 0, "combination_evidence"), get(k, 1, "combination_evidence"), "gpu")
               + lost(get(k, 0, "combination_evidence"), get(k, 1, "combination_evidence"), "reference") for k in changes}
    dropped = {k: n for k, n in dropped.items() if n}
    unchecked = total(lambda k: max(0, get(k, 1, "combination_evidence").get("none", 0) - get(k, 0, "combination_evidence").get("none", 0)))
    never = {k for k in changes if changes[k][2].get("unchecked") or changes[k][2].get("unchecked classes")}

    def where(keys):
        ops = {}
        for arch, op in sorted(keys, key=lambda k: (k[1], order.index(k[0]))):
            ops.setdefault(op, []).append(arch)
        return names([f"{op} ({arch_list(archs, order)})" for op, archs in ops.items()])

    line = [f"- **What changed:** {plural(len(by_op), 'opcode')} on {plural(len({a for a, _ in changes}), 'architecture')}: "
            + names([f"{op} ({arch_list(archs, order)})" for op, archs in sorted(by_op.items())]) + "."]
    sem = ([f"🟢 {plural(gained, 'modifier combination')} ({plural(gained_classes, 'class')}) gained semantics"] if gained or gained_classes else []) + (
        [f"🔴 {plural(sum(fell.values()), 'combination')} lost or weakened semantics in {where(fell)}"] if fell else [])
    line.append("- **Implemented semantics:** " + ("; ".join(sem) if sem else "⚪ no change in semantics") + ".")
    ev = [f"{n:,} {label}" for k, n in checks.items() if n
          for label in [{"gpu": "GPU checks", "ptxas": "PTX compilation", "reference": "reference cases"}[k]]]
    line.append("- **Recorded evidence:** " + (f"🟢 combinations newly recorded with {', '.join(ev)}" if ev else "⚪ no new evidence")
                + (f"; 🔴 {plural(sum(dropped.values()), 'combination')} lost GPU checks or all evidence in {where(dropped)}." if dropped
                   else "; no recorded evidence was lost."))
    gaps = ([f"🟡 {plural(unchecked, 'more modeled combination')} without recorded evidence"] if unchecked else []) + (
        [f"🟡 semantics never run by any evidence in {where(never)}"] if never else [])
    line.append("- **Evidence gaps:** " + ("; ".join(gaps) if gaps else "none added") + ".")
    line.append(f"- **Needs attention:** {plural(len(items), 'item')}" + (" (below)." if items else "."))
    if not detailed:
        line.append("- Form names are unavailable: a compared commit has no readable `coverage.html` data.")
    semantics_rows = [f"| {html.escape(op)} | {arch_list(archs, order)} | " + " | ".join(cells) + " |"
                      for op, archs, cells in grouped(semantics, order)]
    evidence_rows = [f"| {html.escape(op)} | {arch_list(archs, order)} | " + " | ".join(cells) + " |"
                     for op, archs, cells in grouped(evidence, order)]
    news = {k: get(k, 1, "combination_evidence").get("none", 0) - get(k, 0, "combination_evidence").get("none", 0) for k in changes}
    gap_lines = ([f"- 🔴 {html.escape(op)} ({arch_list(archs, order)}): {plural(n, 'combination')} lost GPU checks or all evidence"
                  for op, archs, n in grouped(dropped, order)]
                 + [f"- 🟡 {html.escape(op)} ({arch_list(archs, order)}): {plural(n, 'more modeled combination')} without recorded evidence"
                    for op, archs, n in grouped({k: n for k, n in news.items() if n > 0}, order)])
    attention_lines = [f"{i}. {mark} **{html.escape(op)}** on {archs}: {what}.<br>Next: {next_check}."
                       for i, (mark, op, archs, what, next_check) in enumerate(items, 1)]
    coverage = ("| Architecture | Before | After | Change |\n"
                "| :--- | ---: | ---: | ---: |\n" + "\n".join(rows) + "\n")

    def text(limit=None):
        """The report before the per-architecture details, each list cut to `limit` entries."""
        def cut(entries, what):
            more = len(entries) - limit if limit is not None and len(entries) > limit else 0
            return "\n".join(entries[:len(entries) - more]) + (f"\n\n…and {plural(more, 'more ' + what)} in the full report (`coverage-comment-report`)." if more else "")
        out = header + "### Summary\n\n" + "\n".join(line) + "\n\n### Implemented semantics\n\n"
        out += ("| Opcode | Architectures | Classes with semantics | Combinations with semantics | Forms gained | Classes and forms lost or weakened |\n"
                "| :--- | :--- | ---: | ---: | :--- | :--- |\n" + cut(semantics_rows, "row") + "\n\n"
                if semantics_rows else "No change in implemented semantics.\n\n")
        out += "### Recorded evidence\n\n" + (
            "Modifier combinations by their strongest recorded evidence (classes in parentheses), and the forms that gained each kind.\n\n"
            "| Opcode | Architectures | " + " | ".join(f"{STATUS_COLORS[k]} {EVIDENCE_LABELS[k]}" for k in EVIDENCE_ORDER)
            + " | Forms newly checked |\n| :--- | :--- | " + " | ".join("---:" for _ in EVIDENCE_ORDER) + " | :--- |\n"
            + cut(evidence_rows, "row") + "\n\n" if evidence_rows else "No recorded evidence changes.\n\n")
        out += ("### Evidence gaps\n\n> [!NOTE]\n> Gaps in recorded evidence, not test failures: semantics nothing has checked yet, "
                "or checks no longer recorded for the current forms.\n\n"
                + (cut(gap_lines, "gap") if gap_lines else "No evidence was lost and no semantics were added without evidence.") + "\n\n")
        out += "### Needs attention\n\n" + (cut(attention_lines, "item") if attention_lines else "Nothing needs attention.") + "\n\n"
        out += (("### Opcode coverage\n\n" + coverage) if moved else
                "<details>\n<summary>Opcode coverage: ⚪ unchanged</summary>\n\n" + coverage + "\n</details>\n") + "\n"
        return out + HOW_TO_READ

    summary = text()
    full = summary + "\n".join(sections)
    body = full
    if len(full.encode("utf-8")) > COMMENT_LIMIT:
        notice = (f"\nDetails truncated. Download `coverage-comment-report` from the "
                  f"[CI run]({run_url}) for the full Markdown report.\n")
        body = summary if len((summary + notice).encode("utf-8")) <= COMMENT_LIMIT else text(LIMIT_ROWS)
        for section in sections:
            if len((body + section + notice).encode("utf-8")) > COMMENT_LIMIT:
                break
            body += section
        body += notice
    return {"full": full, "body": body, "changed": True}


def show(sha, path):
    return subprocess.check_output(["git", "show", f"{sha}:{path}"], text=True)


def read_coverage(sha):
    return json.loads(show(sha, COVERAGE))


def read_detail(sha=None):
    """The coverage page's per-class detail at `sha` (the working tree when None), or None when the
    commit has no page with embedded data."""
    try:
        return unpack(show(sha, PAGE) if sha else (ROOT / PAGE).read_text())
    except (subprocess.CalledProcessError, OSError, AttributeError, ValueError, KeyError):
        return None


def read_families(detail, sha=None):
    """{opcode: case family} from `sha`'s evidence.json (the working tree when None), or {}."""
    try:
        return families(json.loads(show(sha, EVIDENCE) if sha else (ROOT / EVIDENCE).read_text()), detail or {})
    except (subprocess.CalledProcessError, OSError, ValueError):
        return {}


def compare(base_sha, head_sha=None, **kwargs):
    """render() of two commits' committed coverage, the head the working tree when None."""
    head = read_coverage(head_sha) if head_sha else json.loads((ROOT / COVERAGE).read_text())
    base_detail, head_detail = read_detail(base_sha), read_detail(head_sha)
    return render(read_coverage(base_sha), head, base_sha, head_sha or "working tree", base_detail=base_detail,
                  head_detail=head_detail, family=read_families(head_detail, head_sha), **kwargs)


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("report", "publish", "local"),
                        help="local: print the report comparing REV with the working tree, outside CI")
    parser.add_argument("rev", nargs="?", default="HEAD")
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.command == "local":
        print(compare(args.rev)["full"])
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
        report = compare(base_sha, head_sha, run_url=run_url, repo=repo)
        args.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")
        (args.directory / "coverage-changes.md").write_text(report["full"], encoding="utf-8")
    else:
        publish(json.loads(path.read_text()), repo, event["number"], base_sha, head_sha)


if __name__ == "__main__":
    main()
