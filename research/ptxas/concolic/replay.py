#!/usr/bin/env python3
"""Compile generated cases, then dump compiler IRs for accepted cases."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ptxas", type=Path, default=Path("/usr/local/cuda-13.0/bin/ptxas"))
    parser.add_argument("--dump-count", type=int, default=3)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = output / "cases"
    cases.mkdir()
    inputs = []
    for path in args.inputs:
        inputs.extend(sorted(p for p in path.iterdir() if p.is_file()) if path.is_dir() else [path])
    seen = set()
    results = []
    dumped = 0
    for path in inputs:
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        directory = cases / digest[:16]
        directory.mkdir()
        seed = directory / "input.ptx"
        seed.write_bytes(data)
        cubin = directory / "kernel.cubin"
        target = [str(args.ptxas.resolve()), "-arch=sm_100", "-o", str(cubin), str(seed)]
        record = {"source": str(path.resolve()), "sha256": digest}
        try:
            native = subprocess.run(target, capture_output=True, timeout=5)
            record.update(returncode=native.returncode, accepted=native.returncode == 0 and cubin.exists())
            (directory / "compile.log").write_bytes(native.stdout + native.stderr)
        except subprocess.TimeoutExpired:
            record.update(returncode=None, accepted=False, timed_out=True)
        if record["accepted"] and dumped < args.dump_count:
            dumped += 1
            env = os.environ.copy()
            env.update({"PTX_IR_OUT": str(directory / "ptx-ir.jsonl"),
                        "DAG_OUT": str(directory / "cop-dag.txt"),
                        "ORI_OUT": str(directory / "ori.txt"),
                        "ORI_PHASES": "all", "ORI_WHEN": "both"})
            record["dumps"] = {}
            for name, script in (("ptx", HERE / "ptx_ir_print.py"),
                                 ("cop", HERE.parent / "dag_print.py"),
                                 ("ori", HERE.parent / "ori_print.py")):
                with (directory / f"gdb-{name}.log").open("wb") as log:
                    try:
                        traced = subprocess.run(["gdb", "-q", "-batch", "-x", str(script),
                                                 "--args", *target], env=env, stdout=log,
                                                stderr=subprocess.STDOUT, timeout=30)
                        record["dumps"][name] = traced.returncode
                    except subprocess.TimeoutExpired:
                        record["dumps"][name] = "timeout"
            nvdisasm = args.ptxas.parent / "nvdisasm"
            with (directory / "sass.txt").open("wb") as log:
                decoded = subprocess.run([str(nvdisasm), str(cubin)], stdout=log,
                                         stderr=subprocess.STDOUT, timeout=10)
            record["disassembly_returncode"] = decoded.returncode
        results.append(record)
    summary = {"unique": len(results), "accepted": sum(r["accepted"] for r in results),
               "rejected": sum(not r["accepted"] for r in results),
               "dumped": dumped, "cases": results}
    (output / "results.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "cases"}))


if __name__ == "__main__":
    main()
