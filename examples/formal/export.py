"""Write a small branching SASS program and its executable K input.

Run from the checkout: uv run python examples/formal/export.py
Then compile/run with the commands in docs/semantics.md, K backend.
"""
from pathlib import Path
from sass.assemble import encoding
from sass.formal import k
from sass import paths


def main():
    code = bytes.fromhex(encoding.encode("SM89", "bra_", sImm=16) +
                         encoding.encode("SM89", "nop_") +
                         encoding.encode("SM89", "exit_"))
    exported = k.export_program("SM89", code)
    destination = paths.CACHE / "formal-example"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "program.k").write_text(exported.source)
    (destination / "input.sass").write_text(k.input_term(exported))
    import json
    (destination / "program.json").write_text(json.dumps(exported.manifest(), indent=2) + "\n")
    print(destination)


if __name__ == "__main__": main()
