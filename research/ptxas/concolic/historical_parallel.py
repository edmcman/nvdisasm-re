#!/usr/bin/env python3
"""Independent AFL and concolic processes for PTX-to-SASS case generation."""
import argparse
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import threading
import time

from corpus import sass_key, token_key

HERE = Path(__file__).resolve().parent


def atomic_write(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def connect(root):
    db = sqlite3.connect(root / "corpus.sqlite", timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    return db


def bounded(argv, env, log, seconds, stop):
    """Wait without holding up other generators; stop the whole child group."""
    with log.open("wb") as output:
        child = subprocess.Popen(argv, env=env, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        end = time.monotonic() + seconds
        while child.poll() is None:
            if stop.is_set() or time.monotonic() >= end:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
                return {"returncode": child.returncode, "interrupted": stop.is_set(),
                        "timed_out": not stop.is_set()}
            stop.wait(0.1)
        return {"returncode": child.returncode, "interrupted": False, "timed_out": False}


def symbolic_interval(data, scope):
    if scope == "file":
        return None
    marker = b"// BEGIN_INSTRUCTION\n"
    start = data.find(marker)
    if start < 0:
        return None  # valid AFL mutations may remove the comment markers
    start += len(marker)
    end = data.find(b"// END_INSTRUCTION", start)
    if end < start:
        return None
    if scope == "opcode":
        token = re.match(rb"[A-Za-z0-9_]+", data[start:end])
        if not token:
            return None
        end = start + len(token.group())
    return start, end


def concolic_worker(root, index, config, stop):
    owner = f"concolic{index:02d}"
    db = connect(root)
    worker_root = root / "concolic" / owner
    worker_root.mkdir()
    while not stop.is_set():
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT key FROM cases WHERE state='accepted' AND owner IS NULL "
                         "ORDER BY first_seen, key LIMIT 1").fetchone()
        if row:
            key = row[0]
            db.execute("UPDATE cases SET owner=? WHERE key=?", (owner, key))
        db.commit()
        if not row:
            stop.wait(0.5)
            continue
        work = worker_root / key
        work.mkdir()
        seed = work / "input.ptx"
        seed.write_bytes((root / "corpus" / f"{key}.ptx").read_bytes())
        generated = work / "generated"
        generated.mkdir()
        env = os.environ.copy()
        for name in ("PTX_OPCODE_DOMAIN", "PTX_SYMBOLIC_BEGIN", "PTX_SYMBOLIC_END",
                     "SYMCC_NO_SYMBOLIC_INPUT", "SYMCC_MEMORY_INPUT"):
            env.pop(name, None)
        env.update(SYMCC_INPUT_FILE=str(seed), SYMCC_OUTPUT_DIR=str(generated))
        interval = symbolic_interval(seed.read_bytes(), config["scope"])
        if interval:
            env.update(PTX_SYMBOLIC_BEGIN=str(interval[0]), PTX_SYMBOLIC_END=str(interval[1]))
        started = time.monotonic()
        argv = [config["symqemu"], config["ptxas"], f'-arch={config["arch"]}',
                "-o", "/dev/null", str(seed)]
        result = bounded(argv, env, work / "solver.log", config["worker_seconds"], stop)
        result.update(input_key=key, symbolic_range=interval,
                      elapsed_seconds=time.monotonic() - started)
        atomic_write(work / "result.json", (json.dumps(result, indent=2) + "\n").encode())
        db.execute("UPDATE cases SET concolic_done=1 WHERE key=?", (key,))
        db.commit()
    db.close()


def save_afl_log(child, destination):
    with destination.open("wb") as log:
        for line in child.stdout:
            if b"Fuzzing test case #" in line or b"Entering queue cycle" in line:
                continue
            if line.strip() not in (b"", b"\x1b[0m"):
                log.write(line)
                log.flush()


def candidates(root):
    for path in sorted((root / "seeds").glob("*.ptx")):
        yield path, "seed"
    for path in sorted((root / "afl").glob("*/queue/id:*")):
        if path.is_file():
            yield path, "afl"
    for path in sorted((root / "concolic").glob("*/*/generated/*")):
        if path.is_file():
            yield path, "concolic"


def collect(root, db, config, stop, deadline):
    """Compile each token-distinct candidate once, outside AFL's hot path."""
    for path, origin in candidates(root):
        if stop.is_set() or time.monotonic() >= deadline:
            break
        if db.execute("SELECT 1 FROM observations WHERE path=?", (str(path),)).fetchone():
            continue
        try:
            before = path.stat()
            if time.time() - before.st_mtime < 0.25:
                continue
            data = path.read_bytes()
            if not data or path.stat().st_size != len(data):
                continue
        except OSError:
            continue
        key = token_key(data)
        db.execute("INSERT INTO observations VALUES (?, ?, ?, ?)",
                   (str(path), key, origin, hashlib.sha256(data).hexdigest()))
        if db.execute("SELECT 1 FROM cases WHERE key=?", (key,)).fetchone():
            db.commit()
            continue
        directory = root / "cases" / key
        directory.mkdir()
        seed = directory / "input.ptx"
        seed.write_bytes(data)
        cubin = directory / "kernel.cubin"
        argv = [config["ptxas"], f'-arch={config["arch"]}', "-o", str(cubin), str(seed)]
        result = bounded(argv, os.environ.copy(), directory / "compile.log",
                         config["compile_seconds"], stop)
        accepted = result["returncode"] == 0 and cubin.exists()
        encoding = None
        if accepted:
            try:
                encoding = sass_key(cubin.read_bytes())
            except (ValueError, IndexError, __import__('struct').error) as error:
                result["encoding_error"] = str(error)
            # Publish only complete, compilable files. This is also AFL's -F source.
            atomic_write(root / "corpus" / f"{key}.ptx", data)
            if encoding and not db.execute("SELECT 1 FROM encodings WHERE key=?", (encoding,)).fetchone():
                nvdisasm = str(Path(config["ptxas"]).parent / "nvdisasm")
                decoded = bounded([nvdisasm, str(cubin)], os.environ.copy(),
                                  root / "sass" / f"{encoding}.txt", 10, stop)
                db.execute("INSERT INTO encodings VALUES (?, ?, ?)",
                           (encoding, key, decoded["returncode"]))
        result.update(accepted=accepted, token_key=key, sass_key=encoding, origin=origin)
        atomic_write(directory / "result.json", (json.dumps(result, indent=2) + "\n").encode())
        db.execute("INSERT INTO cases (key, state, origin, first_seen, compile_rc, sass_key) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (key, "accepted" if accepted else "rejected", origin,
                    time.time(), result["returncode"], encoding))
        db.commit()


def status(root, db, afl, concolic, started, phase):
    rows = dict(db.execute("SELECT state, COUNT(*) FROM cases GROUP BY state"))
    observed = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    distinct = sum(rows.values())
    result = {"phase": phase, "elapsed_seconds": round(time.monotonic() - started, 1),
              "afl_workers_running": sum(p.poll() is None for p in afl),
              "concolic_workers_running": sum(p.is_alive() for p in concolic),
              "observations": observed, "token_distinct_candidates": distinct,
              "whitespace_or_comment_duplicates": observed - distinct,
              "accepted_ptx_variants": rows.get("accepted", 0),
              "rejected_candidates": rows.get("rejected", 0),
              "distinct_sass_encodings": db.execute("SELECT COUNT(*) FROM encodings").fetchone()[0],
              "concolic_inputs_completed": db.execute("SELECT COUNT(*) FROM cases WHERE concolic_done=1").fetchone()[0],
              "afl_exit_codes": [p.poll() for p in afl],
              "concolic_exit_codes": [p.exitcode for p in concolic]}
    atomic_write(root / "status.json", (json.dumps(result, indent=2) + "\n").encode())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tools", type=Path, default=HERE / "tools")
    parser.add_argument("--ptxas", type=Path, default=Path("/usr/local/cuda-13.0/bin/ptxas"))
    parser.add_argument("--arch", default="sm_100")
    parser.add_argument("-n", "--afl-workers", type=int, default=8)
    parser.add_argument("-m", "--concolic-workers", type=int, default=1)
    parser.add_argument("--seconds", type=int, default=3600)
    parser.add_argument("--worker-seconds", type=int, default=30)
    parser.add_argument("--compile-seconds", type=int, default=3)
    parser.add_argument("--scope", choices=("instruction", "opcode", "file"), default="instruction")
    parser.add_argument("--seed", type=Path, action="append")
    args = parser.parse_args()
    if args.afl_workers < 1 or args.concolic_workers < 0 or min(args.seconds, args.worker_seconds, args.compile_seconds) < 1:
        parser.error("n must be positive, m nonnegative, and time limits positive")
    root = args.output.resolve()
    tools = args.tools.resolve()
    config = {"ptxas": str(args.ptxas.resolve()), "arch": args.arch,
              "symqemu": str(tools / "symqemu/build/symqemu-x86_64"),
              "scope": args.scope, "worker_seconds": args.worker_seconds,
              "compile_seconds": args.compile_seconds, "afl_workers": args.afl_workers,
              "concolic_workers": args.concolic_workers, "seconds": args.seconds}
    for executable in (tools / "AFLplusplus/afl-fuzz", args.ptxas,
                       *([Path(config["symqemu"])] if args.concolic_workers else [])):
        if not os.access(executable, os.X_OK):
            parser.error(f"missing executable: {executable}")
    root.mkdir(parents=True, exist_ok=False)
    for name in ("seeds", "afl", "corpus", "concolic", "cases", "sass", "logs"):
        (root / name).mkdir()
    for index, seed in enumerate(args.seed or [HERE / "instruction.ptx"]):
        (root / "seeds" / f"seed{index:03d}.ptx").write_bytes(seed.read_bytes())
    db = connect(root)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE cases (key TEXT PRIMARY KEY, state TEXT, origin TEXT,
            first_seen REAL, compile_rc INTEGER, sass_key TEXT,
            owner TEXT, concolic_done INTEGER DEFAULT 0);
        CREATE TABLE observations (path TEXT PRIMARY KEY, key TEXT, origin TEXT, raw_sha TEXT);
        CREATE TABLE encodings (key TEXT PRIMARY KEY, ptx_key TEXT, decode_rc INTEGER);
    """)
    atomic_write(root / "config.json", (json.dumps(config, indent=2) + "\n").encode())
    context = mp.get_context("spawn")
    stop = context.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    afl, concolic, readers = [], [], []
    started = time.monotonic()
    deadline = started + args.seconds
    env = os.environ.copy()
    for name in list(env):
        if name.startswith(("AFL_CUSTOM_", "AFL_PYTHON_", "PTX_SYMBOLIC_", "SYMCC_", "SYMQEMU_")) or name in (
                "PTX_OPCODE_DOMAIN", "PTX_VALIDATE_GENERATED", "AFL_EXIT_WHEN_DONE", "AFL_AUTORESUME"):
            env.pop(name)
    env.update(AFL_PATH=str(tools / "AFLplusplus"), AFL_MAP_SIZE="1048576",
               AFL_QEMU_MAP_SIZE="1048576", AFL_NO_UI="1", AFL_NO_AFFINITY="1",
               AFL_SKIP_CPUFREQ="1", AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES="1",
               AFL_IGNORE_UNKNOWN_ENVS="1", AFL_DISABLE_TRIM="1", AFL_SYNC_TIME="1",
               AFL_IMPORT_FIRST="1")
    try:
        for index in range(args.afl_workers):
            name = f"afl{index:02d}"
            role = ["-M", name, "-F", str(root / "corpus")] if index == 0 else ["-S", name]
            argv = [str(tools / "AFLplusplus/afl-fuzz"), "-Q", *role, "-z",
                    "-m", "none", "-t", "2000", "-V", str(args.seconds),
                    "-i", str(root / "seeds"), "-o", str(root / "afl"),
                    "-x", str(HERE / "ptx.dict"), "--", config["ptxas"],
                    f'-arch={args.arch}', "-o", "/dev/null", "@@"]
            atomic_write(root / "logs" / f"{name}.command.json", (json.dumps(argv) + "\n").encode())
            child = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            afl.append(child)
            reader = threading.Thread(target=save_afl_log,
                                      args=(child, root / "logs" / f"{name}.log"), daemon=True)
            reader.start()
            readers.append(reader)
        for index in range(args.concolic_workers):
            worker = context.Process(target=concolic_worker, args=(root, index, config, stop))
            worker.start()
            concolic.append(worker)
        atomic_write(root / "processes.json", (json.dumps({"supervisor_pid": os.getpid(),
                    "afl_pids": [p.pid for p in afl], "concolic_pids": [p.pid for p in concolic]}) + "\n").encode())
        print(f"Running {len(afl)} AFL and {len(concolic)} concolic processes; {root}", flush=True)
        next_report = started
        while not stop.is_set() and time.monotonic() < deadline:
            collect(root, db, config, stop, deadline)
            if time.monotonic() >= next_report:
                progress = status(root, db, afl, concolic, started, "running")
                print(json.dumps(progress), flush=True)
                next_report = time.monotonic() + 15
            if not any(p.poll() is None for p in afl):
                raise RuntimeError("all AFL processes exited; inspect logs")
            if concolic and not any(p.is_alive() for p in concolic):
                raise RuntimeError("all concolic processes exited; inspect logs")
            stop.wait(0.5)
    finally:
        stop.set()
        for child in afl:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGINT)
        for child in afl:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for worker in concolic:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join()
        for reader in readers:
            reader.join(timeout=1)
        progress = status(root, db, afl, concolic, started, "stopped")
        print(json.dumps(progress), flush=True)
        db.close()


if __name__ == "__main__":
    main()
