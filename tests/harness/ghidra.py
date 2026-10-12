"""Batch requests through the Ghidra-side programs, which run under the pyghidra venv."""
import json, os, subprocess
from concurrent.futures import ThreadPoolExecutor
from tests.paths import GHIDRA_PY, LDEFS, ROOT, language


def run(program, arch, fp, lines, *opts):
    # cwd="/" keeps repository modules from shadowing the venv's imports.
    p = subprocess.run([GHIDRA_PY, str(ROOT / "tests" / program), str(LDEFS), language(arch, fp), *opts],
                       input="".join(line + "\n" for line in lines), capture_output=True, text=True, cwd="/")
    results = [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]
    if p.returncode or len(results) != len(lines):
        raise RuntimeError(f"{program} {language(arch, fp)}: {len(results)}/{len(lines)} results\n{p.stderr[-3000:]}")
    return results


def emulate(arch, fp, requests):
    return run("ghidra_emulate.py", arch, fp, [json.dumps(r) for r in requests]) if requests else []


def disassemble(arch, fp, words, *opts):
    """words: [(word bytes, address)]."""
    return run("ghidra_decode.py", arch, fp, [f"{a:x} {w.hex()}" for w, a in words], *opts) if words else []


def concurrently(fn, jobs):
    """{key: args} -> {key: fn(*args)}, one Ghidra process per job."""
    with ThreadPoolExecutor(max(1, min(os.cpu_count() or 1, 18, len(jobs)))) as pool:
        futures = {key: pool.submit(fn, *args) for key, args in jobs.items()}
    return {key: f.result() for key, f in futures.items()}


def outputs(result):
    return [int.from_bytes(bytes.fromhex(x), "little") for x in result["outputs"]]
