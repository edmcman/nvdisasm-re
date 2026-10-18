"""Own ordinary legal-form coverage through reference-backed shared-case plans.

python -m tests.autocases SM89 --manifest-dir DIR [--write] [--report gaps.json]
"""
import argparse
import itertools
import json
from collections import Counter
from pathlib import Path
from sass.paths import LANGUAGES
from sass.decode import pydecode
from sass.sleigh.build import ARCHES
from sass.sleigh.modifiers import settings
from tests.cases import emulator_cases
from tests.harness import autocases
from tests.harness import autocase_profiles, autocase_extended


def forms(row):
    """Expand the factored legal table, including modifiers that do not affect status."""
    c = row.get('combinations')
    if not c:
        raise ValueError(f"{row['cls']}: missing combination table; regenerate with --coverage")
    names = list(c['labels'])
    free = [n for n in names if n not in c['decides']]
    for *labels, status in c['table']:
        fixed = dict(zip(c['decides'], labels))
        for values in itertools.product(*(c['labels'][n] for n in free)):
            yield {**fixed, **dict(zip(free, values))}, status


def identity(cls, form): return cls, tuple(sorted(form.items()))


def plan(arch, rows):
    # Ignore prior generated plans: rerunning must reproduce them, not delete them.
    manual = emulator_cases(arch, 'exact', exhaustive=False, generated=False)
    covered = set()
    for cases in manual.values():
        for case in cases:
            if not case.request.get('word') or 'error' in case.expected or '__opaque' in case.expected: continue
            d = pydecode.decode(arch, bytes.fromhex(case.request['word']))
            covered.add(identity(d.klass.name, settings(arch, d)))
    entries, gaps, counts = [], [], Counter()
    for row in rows:
        c = row.get('combinations')
        if c is None: raise ValueError(f"{row['cls']}: missing combination table; regenerate with --coverage")
        if row['opcode'] not in autocases.OWNED:
            total = len(c['table']) * c['free']
            supported = sum(c['free'] for r in c['table'] if r[-1] in ('native', 'primitive'))
            lookup = {tuple(r[:-1]): r[-1] for r in c['table']}
            ran = sum(lookup.get(tuple(dict(f).get(n) for n in c['decides'])) in ('native', 'primitive')
                      and set(dict(f)) == set(c['labels'])
                      and all(v in c['labels'][n] for n, v in f)
                      for cls, f in covered if cls == row['cls'])
            counts.update(legal=total, unresolved_semantics=total-supported, manual=ran, blocked=supported-ran)
            if supported > ran:
                gaps.append(dict(cls=row['cls'], count=supported-ran, reason='needs a reference/operand adapter'))
            if total > supported:
                gaps.append(dict(cls=row['cls'], count=total-supported, reason='unresolved semantics'))
            continue
        blocked_reasons = Counter()
        for form, status in forms(row):
            counts['legal'] += 1
            if status not in ('native', 'primitive'):
                counts['unresolved_semantics'] += 1
                gaps.append(dict(cls=row['cls'], settings=form, reason='unresolved semantics'))
                continue
            # Retained regressions supplement ordinary coverage; they must not suppress it.
            profiles = autocase_profiles.names(row['opcode'], row['cls'], form)
            try:
                reason = autocase_extended.blocked_reason(row['opcode'],form)
                if reason: raise autocases.Unsupported(reason)
                autocases.generate(arch, row['cls'], form, profiles=profiles, native=status == 'native')
            except (autocases.Unsupported, ValueError) as error:
                counts['blocked'] += 1
                if row['opcode'] in autocase_extended.OPS:
                    blocked_reasons[str(error)] += 1
                else: gaps.append(dict(cls=row['cls'], settings=form, reason=str(error)))
            else:
                counts['generated'] += 1
                entries.append(dict(family=autocases.FAMILIES[row['opcode']], cls=row['cls'], settings=form,
                                    profiles=profiles, native=status == 'native'))
        gaps.extend(dict(cls=row['cls'], count=count, reason=reason) for reason,count in blocked_reasons.items())
    return dict(version=2, arch=arch, forms=entries), dict(counts=counts, gaps=gaps)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('arch', choices=ARCHES)
    p.add_argument('--manifest-dir', type=Path, default=LANGUAGES)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--write', action='store_true', help='replace the shared-case form plan')
    mode.add_argument('--check', action='store_true', help='fail if the saved plan differs from regeneration')
    p.add_argument('--report', type=Path, help='write remaining adapter gaps as JSON')
    a = p.parse_args()
    path = a.manifest_dir / f'sass_{a.arch.lower()}_coverage.json'
    if not path.exists(): p.error(f'{path} missing; run sass-build-languages {a.arch} --fp exact --generate-only --coverage')
    generated, report = plan(a.arch, json.loads(path.read_text()))
    target = autocases.PLANS / (a.arch + '.json')
    if a.check and (not target.exists() or json.loads(target.read_text()) != generated):
        p.error(f'{target} is stale; rerun with --write')
    if a.write:
        autocases.PLANS.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix('.tmp')
        tmp.write_text(json.dumps(generated, indent=2) + '\n'); tmp.replace(target)
    if a.report: a.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['counts'], sort_keys=True))


if __name__ == '__main__': main()
