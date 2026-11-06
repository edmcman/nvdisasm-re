# /// script
# requires-python = ">=3.13"
# dependencies = ["lz4", "z3-solver==5.3.3"]
# ///
import argparse
import hashlib
import json
import os
import platform
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from sass.decode import cubin
import type_suite


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('corpus', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--contexts', type=Path, default=Path(__file__).with_name('experiment_contexts.json'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    os.environ['SHAPE_CORPUS'] = str(args.corpus)
    directory = Path(__file__).parent
    start = time.monotonic()
    cases = unittest.defaultTestLoader.discover(str(directory), pattern='test_*.py')
    with (args.output / 'tests.txt').open('w') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(cases)
    print(f'Tests: {result.testsRun}, {time.monotonic() - start:.1f}s', flush=True)
    if not result.wasSuccessful() or result.skipped: raise RuntimeError('research tests failed or skipped; see tests.txt')
    contexts, reports = json.loads(args.contexts.read_text()), []
    for path in sorted(args.corpus.glob('*/*_sm90.elf')):
        for position, entry in enumerate(cubin.text_sections(path.read_bytes())):
            name = entry.removeprefix('.text.')
            label = path.parent.name if position == 0 else f'{path.parent.name}.{name}'
            start = time.monotonic()
            report = type_suite.run(path, args.output, entry=name, ranges=contexts.get(name), label=label)
            reports.append(report)
            print(name, f'{time.monotonic() - start:.1f}s',
                  [(t['buffer'], t['dimensions'], t['dimensions_recovered']) for t in report.get('shapes', {}).get('tensors', [])], flush=True)
    manifest = {'python': platform.python_version(),
                'tests': {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped)},
                'contexts': contexts,
                'source_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob('*.py'))},
                'entries': reports, 'all_entry_types_analyzed': all(r['status'] != 'unsupported' for r in reports),
                'integer_interpretation_proven': False, 'source_tensor_rank_inferred': False,
                'scope': 'Common SSA equality/inequality fixed point; conditional tensor dimensions; no layout recognizers',
                'legacy_tests_included': False}
    (args.output / 'summary.json').write_text(json.dumps(reports, indent=2) + '\n')
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(args.output / 'manifest.json')


if __name__ == '__main__': main()
