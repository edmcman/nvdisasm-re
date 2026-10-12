"""Run the HMNMX2 semantic cases in exact/readable languages by architecture."""
import argparse
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]
from test_semantics import cases, run

ARCHES = ('SM75', 'SM80', 'SM86', 'SM89', 'SM90', 'SM100', 'SM101', 'SM103', 'SM120')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('architectures', nargs='*')
    args = parser.parse_args()
    invalid = [arch for arch in args.architectures if arch not in ARCHES]
    if invalid:
        parser.error('unsupported architecture(s): ' + ', '.join(invalid))
    for arch in args.architectures or ARCHES:
        for fp in ('exact', 'readable'):
            todo = [item for item in cases(arch, fp) if item[0].startswith('hmnmx2')]
            run(arch, fp, todo)


if __name__ == '__main__':
    main()
