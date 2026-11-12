import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from infer_types import kernel
from type_system import InferenceError
from common_shapes import CommonShapes
from sass.decode import cubin
import memory_types


def run(path, output, entry=None, ranges=None, label=None):
    label = label or path.parent.name
    result = {"kernel": path.parent.name, "entry": entry, "artifact": label, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    try:
        inference, memory = memory_types.infer(kernel(path, entry), ranges)
        report = inference.report()
        (output / f"{label}.json").write_text(json.dumps(report, indent=2) + "\n")
        (output / f"{label}.memory.json").write_text(json.dumps(memory.report(), indent=2) + "\n")
        result["memory"] = {"cells": len(memory.cells), "residuals": len(memory.residuals)}
        result["verified_returns"] = len(inference.graph.verified_returns)
        analysis = CommonShapes(inference, ranges)
        shape_report = analysis.report()
        (output / f"{label}.shape.json").write_text(json.dumps(shape_report, indent=2) + "\n")
        (output / f"{label}.constraints.json").write_text(json.dumps(analysis.analysis.report(), indent=2) + "\n")
        result["shapes"] = shape_report
        return {**result, "status": "partial", "pointer_inputs": {k: v for k, v in report["inputs"].items() if v.startswith("ptr")},
                "accesses": len(report["accesses"]), "residuals": dict(Counter(r["kind"] for r in report["residuals"])),
                "effects": dict(Counter(e["effect"] for e in report["unsupported"])), "bounds_checked": False}
    except InferenceError as e:
        return {**result, "status": "unsupported", "diagnostic": str(e)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--all-entries", action="store_true")
    parser.add_argument("--contexts", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    contexts = json.loads(args.contexts.read_text()) if args.contexts else {}
    results = []
    for path in sorted(args.corpus.glob("*/*_sm90.elf")):
        entries = [name.removeprefix(".text.") for name in cubin.text_sections(path.read_bytes())]
        for index, entry in enumerate(entries if args.all_entries else entries[:1]):
            label = path.parent.name if index == 0 else f"{path.parent.name}.{entry}"
            context = contexts.get(entry, contexts.get(path.parent.name))
            results.append(run(path, args.output, entry, context, label))
    (args.output / "summary.json").write_text(json.dumps(results, indent=2) + "\n")
    for r in results: print(r["entry"], r["status"], r.get("pointer_inputs", r.get("diagnostic")))


if __name__ == "__main__": main()
