"""Own ordinary legal-form coverage through reference-backed shared-case plans.

python -m tests.autocases SM89 [--manifest-dir DIR] [--write | --check] [--report gaps.json]
"""
import argparse
import json
from collections import Counter
from pathlib import Path
from sass.paths import LANGUAGES
from sass.decode import pydecode
from sass.sleigh.build import ARCHES
from sass.sleigh.modifiers import settings
from sass.sleigh import verification
from tests.cases import emulator_cases
from tests.harness import autocases
from tests.harness import autocase_profiles, autocase_extended, autocase_conversions, autocase_packed_fp, autocase_wide_fp, autocase_system, autocase_mixed_fp, autocase_remaining, autocase_partial


def forms(row):
    """Expand the full legal table, for diagnostics and exhaustive-policy checks."""
    if not row.get('combinations'):
        raise ValueError(f"{row['cls']}: missing combination table; regenerate with --coverage")
    yield from verification.forms(row)


def identity(cls, form): return cls, tuple(sorted(form.items()))


def plan(arch, rows, policy=verification.POLICY):
    # Retained manual regressions supplement, rather than suppress, owned coverage.
    manual = emulator_cases(arch, 'exact', sample=0, generated=False) if any(
        row['opcode'] not in autocases.OWNED for row in rows) else {}
    covered = {}
    for cases in manual.values():
        for case in cases:
            if not case.request.get('word') or 'error' in case.expected or '__opaque' in case.expected: continue
            d = pydecode.decode(arch, bytes.fromhex(case.request['word']))
            covered.setdefault(d.klass.name, []).append(settings(arch, d))
    entries, gaps, counts = [], [], Counter()
    for row in rows:
        c = row.get('combinations')
        if c is None: raise ValueError(f"{row['cls']}: missing combination table; regenerate with --coverage")
        statuses = verification.SUPPORTED | ({'partially native'} if row['opcode'] in autocase_partial.ROWS else set())
        requirements = verification.Requirements(row, policy, statuses)
        counts['legal'] += len(c['table']) * c['free']
        unresolved = sum(c['free'] for e in c['table'] if e[-1] not in verification.SUPPORTED)
        counts['unresolved_semantics'] += unresolved
        counts['requirements'] += requirements.required.bit_count()
        if unresolved:
            gaps.append(dict(cls=row['cls'], count=unresolved, reason='unresolved semantics'))
        if row['opcode'] not in autocases.OWNED:
            ran = 0
            for form in covered.get(row['cls'], ()):
                if requirements.compatible(form):
                    ran |= requirements.mask(form)
            counts['manual'] += ran.bit_count()
            missing = (requirements.required & ~ran).bit_count()
            counts['blocked'] += missing
            counts['covered_requirements'] += ran.bit_count()
            if missing:
                deferred = (requirements.required & ~requirements.plannable & ~ran).bit_count()
                if deferred:
                    gaps.append(dict(cls=row['cls'], count=deferred, reason='partial operand adapter unavailable', unit='interactions'))
                if missing > deferred:
                    gaps.append(dict(cls=row['cls'], count=missing-deferred, reason='needs a reference/operand adapter', unit='interactions'))
            continue
        cover = verification.Cover(requirements)
        blocked_reasons, selected = Counter(), []
        roles = row.get('modifier_roles', {})
        while candidate := cover.next():
            form, status, mask = candidate
            native = status in ('native', 'partially native')
            profiles = autocase_profiles.names(row['opcode'], row['cls'], form)
            try:
                reason = autocase_remaining.blocked_reason(row['opcode'],row['cls'],form) or autocase_extended.blocked_reason(row['opcode'],form) or autocase_packed_fp.blocked_reason(row['opcode'],form,row['cls']) or autocase_wide_fp.blocked_reason(row['opcode'],form)
                if reason: raise autocases.Unsupported(reason)
                autocases.generate(arch, row['cls'], form, profiles=profiles, native=native, sample=0)
                # Every neutral variant must have a usable minimal floor, even
                # when this particular form becomes the full-profile anchor.
                if any(roles.get(n, 'value') != 'value' for n in form):
                    autocases.generate(arch, row['cls'], form, profiles=profiles, native=native,
                                       sample=0, profile_mode='minimal')
            except (autocases.Unsupported, ValueError) as error:
                blocked_reasons[str(error)] += 1
            else:
                cover.accept(mask)
                selected.append(dict(family=autocases.FAMILIES[row['opcode']], cls=row['cls'], settings=form,
                                     profiles=profiles, native=native, strength=policy.strength(row)))
        seen = set()
        for entry in sorted(selected, key=lambda e: identity(e['cls'], e['settings'])):
            projection = tuple((n, v) for n, v in sorted(entry['settings'].items()) if roles.get(n, 'value') == 'value')
            entry['profile_mode'] = 'minimal' if projection in seen else 'full'
            seen.add(projection)
            entries.append(entry)
        counts['generated'] += len(selected)
        counts['covered_requirements'] += (requirements.required & ~cover.uncovered).bit_count()
        counts['blocked'] += cover.uncovered.bit_count()
        if cover.uncovered:
            deferred = (cover.uncovered & ~requirements.plannable).bit_count()
            if deferred:
                gaps.append(dict(cls=row['cls'], count=deferred, reason='partial operand adapter unavailable', unit='interactions'))
            missing = (cover.uncovered & requirements.plannable).bit_count()
            if missing:
                gaps.append(dict(cls=row['cls'], count=missing, reason='no usable reference/operand adapter', unit='interactions',
                                 attempts=dict(sorted(blocked_reasons.items()))))
    return dict(version=3, arch=arch, verification=dict(default_strength=policy.default_strength,
                opcode_strength=policy.opcode_strength), forms=entries), dict(counts=counts, gaps=gaps)


def plan_text(plan):
    """Keep large generated matrices reviewable: one complete form per line, same JSON schema."""
    entries=',\n'.join('    '+json.dumps(e,separators=(',',':')) for e in plan['forms'])
    header = json.dumps({k:v for k,v in plan.items() if k != 'forms'}, indent=2)
    return header[:-2]+',\n  "forms": [\n'+entries+'\n  ]\n}\n'




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
        tmp.write_text(plan_text(generated)); tmp.replace(target)
    if a.report: a.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['counts'], sort_keys=True))


if __name__ == '__main__': main()
