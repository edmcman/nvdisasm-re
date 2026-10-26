"""Report every partial native class's operand contract, selected forms and blockers."""
import argparse
import json
from pathlib import Path
from sass.paths import LANGUAGES
from sass.sleigh.build import ARCHES
from tests.harness import autocases, autocase_partial


def audit(arch, manifests=LANGUAGES, plans=autocases.PLANS):
    rows = json.loads((manifests / f'sass_{arch.lower()}_coverage.json').read_text())
    plan = json.loads((plans / f'{arch}.json').read_text())
    report = json.loads((plans / f'{arch}.gaps.json').read_text())
    selected, gaps = {}, {}
    for entry in plan['forms']:
        selected.setdefault(entry['cls'], []).append(entry)
    for gap in report['gaps']:
        if gap.get('unit') == 'interactions':
            gaps.setdefault(gap['cls'], []).append(gap)
    result = []
    for row in rows:
        c = row.get('combinations', {})
        partial = sum(c['free'] for entry in c.get('table', []) if entry[-1] == 'partially native')
        if not partial:
            continue
        statuses = {tuple(entry[:-1]): entry[-1] for entry in c['table']}
        entries = [entry for entry in selected.get(row['cls'], [])
                   if statuses[tuple(entry['settings'][n] for n in c['decides'])] == 'partially native']
        result.append(dict(cls=row['cls'], opcode=row['opcode'], partial_combinations=partial,
                           operand_rows=autocase_partial.ROWS.get(row['opcode']),
                           selected_partial_forms=len(entries),
                           full_profiles=sum(entry['profile_mode'] == 'full' for entry in entries),
                           gaps=gaps.get(row['cls'], [])))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('architectures', nargs='*', choices=ARCHES)
    parser.add_argument('--manifest-dir', type=Path, default=LANGUAGES)
    parser.add_argument('--plan-dir', type=Path, default=autocases.PLANS)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    text = json.dumps({arch: audit(arch, args.manifest_dir, args.plan_dir)
                       for arch in args.architectures or ARCHES}, indent=2) + '\n'
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
