"""Batch requests through the Ghidra-side programs, which run under the pyghidra venv."""
import json, os, subprocess
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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
    """{key: args} -> {key: fn(*args)}, one Ghidra process per job. Processes, not threads: jobs
    decode and compare in Python around their Ghidra call, which threads would serialize."""
    with ProcessPoolExecutor(max(1, min(os.cpu_count() or 1, 18, len(jobs)))) as pool:
        futures = {key: pool.submit(fn, *args) for key, args in jobs.items()}
    return {key: f.result() for key, f in futures.items()}


def outputs(result):
    return [int.from_bytes(bytes.fromhex(x), "little") for x in result["outputs"]]


def emulate_sharded(batches, workers=None):
    """{key: (arch, fp, requests)} -> {key: results}, with every batch split into contiguous
    chunks of about equal size so `workers` processes stay busy (default: CPUs, at most 24,
    for about 2 GB per JVM)."""
    workers = workers or max(1, min(os.cpu_count() or 1, 24))
    total = sum(len(r) for _, _, r in batches.values())
    size = max(1, -(-total // workers))
    jobs = {(key, i): (arch, fp, requests[i:i + size]) for key, (arch, fp, requests) in batches.items()
            for i in range(0, max(len(requests), 1), size)}
    with ThreadPoolExecutor(min(workers, len(jobs)) or 1) as pool:
        futures = {job: pool.submit(emulate, *args) for job, args in jobs.items()}
    return {key: [r for (k, i), f in sorted(futures.items(), key=lambda kv: kv[0][1]) if k == key for r in f.result()] for key in batches}
