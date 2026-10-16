"""Command-line SASS assembler."""
import argparse
import sys
from pathlib import Path

from sass.assemble import elf, source
from sass.assemble.instruction import AssemblyError


def main(argv=None):
    parser = argparse.ArgumentParser(description='Assemble SASS into raw code or a runnable cubin.')
    parser.add_argument('input', help='SASS source file, or - for stdin')
    parser.add_argument('-o', '--output', required=True, help='output file, or - for stdout')
    parser.add_argument('--format', choices=('raw', 'cubin'), default='cubin')
    parser.add_argument('--arch', help='target machine-description architecture, e.g. SM89')
    parser.add_argument('--base', type=lambda s: int(s, 0), default=0, help='raw-code base address')
    args = parser.parse_args(argv)
    try:
        text = sys.stdin.read() if args.input == '-' else Path(args.input).read_text()
        if args.format == 'raw':
            data = source.raw(args.arch, text, args.base, args.input)
        else:
            if args.base:
                parser.error('--base is only valid for raw output')
            data = elf.build(source.module(text, args.arch, args.input), args.input)
        if args.output == '-':
            sys.stdout.buffer.write(data)
        else:
            Path(args.output).write_bytes(data)
    except (AssemblyError, OSError, ValueError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
