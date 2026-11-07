"""Write a small branching SASS program and its executable K input.

Run from the checkout: uv run python examples/formal/export.py
Then compile/run with the commands in docs/semantics.md, K backend.
"""
from sass.formal.k import example
from sass import paths


def main():
    worked = example.make()
    exported = worked.exported
    destination = paths.CACHE / "formal-example"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "program.k").write_text(exported.source)
    (destination / "input.sass").write_text(worked.input(0))
    import json
    (destination / "program.json").write_text(json.dumps(exported.manifest(), indent=2) + "\n")
    print(destination)


if __name__ == "__main__": main()
