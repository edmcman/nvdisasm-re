"""Export raw SASS instruction bytes to a K module and its contract manifest."""
import argparse
import json
from pathlib import Path
import sys
from sass.formal import k


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--base", type=lambda s: int(s, 0), default=0x100000)
    parser.add_argument("--fp", choices=("exact", "readable"), default="exact")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output == args.output.with_suffix(".json"):
        parser.error("source output would collide with its .json manifest")
    try:
        exported = k.export_program(args.arch, args.input.read_bytes(), args.base, args.fp)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output.write_text(exported.source)
    args.output.with_suffix(".json").write_text(json.dumps(exported.manifest(), indent=2) + "\n")
    print(f"{len(exported.instructions)} instructions; {len(exported.contracts)} contracts",
          file=sys.stderr)


if __name__ == "__main__": main()
