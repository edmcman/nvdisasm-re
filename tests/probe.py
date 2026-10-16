"""Observe encoded single-instruction scenarios without reference expectations.

python -m tests.probe probes.json [--out observations.json]
Input is a JSON list of Cases.probe arguments (cls, fields, registers, observe,
optional memory/observe_memory/context/hardware). SM89 only; run outside the sandbox.
"""
import argparse
import json
from pathlib import Path
from tests.harness import device_cases
from tests.harness.case import Cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    builder = Cases('SM89')
    for spec in json.loads(args.input.read_text()): builder.probe(**spec)
    result = device_cases.observe(builder)
    text = json.dumps(result, indent=1) + '\n'
    if args.out: args.out.write_text(text)
    else: print(text, end='')
    if any('error' in row for row in result['observations']): raise SystemExit(1)


if __name__ == '__main__': main()
