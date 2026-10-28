"""Batch requests through the Ghidra-side programs, which run under the pyghidra venv. Results are
cached per language and program (see `stamp`), so reruns only send requests Ghidra hasn't answered."""
import ast, collections, hashlib, json, os, subprocess, tempfile, threading
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import cache as memoize
from pathlib import Path
from sass.paths import CACHE, GHIDRA_PY, LANGUAGES, LDEFS, ROOT, language, variant
from sass.sqlcache import open_cache
from sass.workers import fitting

JVM_GIB = 2   # per Ghidra-side process (0.7-2 GB): a 4-CPU, 16 GB CI runner held 24 and was killed (2026-10-22)


def jvm_workers(*limits):
    """Ghidra-side processes to run at once: one per CPU, at most 24, as many as fit in memory."""
    return fitting(JVM_GIB, os.cpu_count() or 1, 24, *limits)

SOURCES = Path(__file__).resolve().parents[2]  # the directory holding sass


def module_file(module):
    p = SOURCES / module.replace(".", "/")
    return next((f for f in (p.with_suffix(".py"), p / "__init__.py") if f.exists()), None)


# Generator-only modules: what they produce reaches Ghidra only through the hashed .sla,
# and the Ghidra-side programs never load them (test_ghidra_programs_skip_generators).
GENERATORS = ("sass.semantics.families", "sass.semantics.operations", "sass.semantics.operands", "sass.sleigh.lower", "sass.sleigh.build")


def generator(module): return any(module == g or module.startswith(g + ".") for g in GENERATORS)


@memoize
def sources(program):
    """The sass sources `program` (a module of this package) can run: its transitive sass imports,
    function-level ones included, with their packages, and this package's Java. The programs run
    as scripts beside each other, so a bare import may name a sibling module, and this package
    itself (its __init__, host-side batching) is never imported."""
    seen, todo = set(), [f"{__name__}.{Path(program).stem}"]
    while todo:
        if (module := todo.pop()) in seen or generator(module) or not (f := module_file(module)): continue
        seen.add(module)
        parts = module.split(".")
        package = parts if f.name == "__init__.py" else parts[:-1]
        if not module.startswith(__name__ + "."): todo += [".".join(parts[:i]) for i in range(1, len(parts))]
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Import): todo += [x for a in n.names for x in (a.name, f"{__name__}.{a.name}")]
            elif isinstance(n, ast.ImportFrom):
                base = ".".join(package[:len(package) - n.level + 1] + [n.module or ""]).strip(".") if n.level else n.module
                todo += [base, f"{__name__}.{base}", *(f"{base}.{a.name}" for a in n.names)]
    from sass.emulate import build
    compiled = set(build.SOURCES) if "sass.emulate.build" in seen else set()   # the C++ the runtime's primitives run
    return sorted({module_file(m) for m in seen} | set(Path(__file__).parent.glob("*.java")) | compiled)


def language_entry(name):
    """The language's own <language> element; the rest of sass.ldefs lists whichever others are built."""
    if not LDEFS.exists(): return b""
    entry = next((e for e in ET.parse(LDEFS).getroot() if e.get("variant") == name), None)
    if entry is None: return b""
    entry.tail = None   # the whitespace before the next entry, if any
    return ET.tostring(entry)


def stamp(program, arch, fp):
    """Hash of everything a Ghidra-side answer depends on: the compiled language, its ldefs entry and
    specs, the sass sources the program runs and the Ghidra install. Paths are relative to the
    checkout or language directory, so identical files in scratch builds agree too."""
    name = variant(arch, fp)
    entry = language_entry(name)
    specs = ET.fromstring(entry) if entry else None
    pspec = specs.get("processorspec") if specs is not None else "sass.pspec"
    compiler = specs.find("compiler") if specs is not None else None
    cspec = compiler.get("spec") if compiler is not None else "sass.cspec"
    files = [LANGUAGES / f"sass_{name}.sla", LANGUAGES / pspec, LANGUAGES / cspec, *sources(program)]
    h = hashlib.sha256(GHIDRA_PY.encode() + os.environ.get("GHIDRA_INSTALL_DIR", "").encode() + entry)
    for f in files:
        key = Path("languages") / f.relative_to(LANGUAGES) if f.is_relative_to(LANGUAGES) else f.relative_to(ROOT)
        h.update(str(key).encode()); h.update(f.read_bytes() if f.exists() else b"")
    return name, h.hexdigest()[:16]


def cache(program, arch, fp, opts):
    """The sqlite result cache for (program, options) on one language; stale stamps are removed."""
    name, key = stamp(program, arch, fp)
    prefix = f"{Path(program).stem}{''.join(opts)}-{name}-"
    path = CACHE / "ghidra" / f"{prefix}{key}.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)

    def remove_stale():
        # Only other stamps: a concurrent process may already have created (and opened) this one,
        # and unlinking its database or WAL under it gives "no such table" or "disk I/O error".
        if path.exists(): return
        for stale in path.parent.glob(f"{prefix}*.sqlite*"):
            if not stale.name.startswith(path.name): stale.unlink(missing_ok=True)
    # Every worker opens a language's new database together.
    return open_cache(path, "results (request text primary key, result text)", path.parent / ".lock", 600, remove_stale)


def run(program, arch, fp, lines, *opts):
    """One JSON result per input line."""
    db = cache(program, arch, fp, opts)
    known = {r: v for i in range(0, len(lines), 900)
             for r, v in db.execute(f"select request, result from results where request in ({','.join('?' * len(lines[i:i + 900]))})", lines[i:i + 900])}
    missing = list(dict.fromkeys(line for line in lines if line not in known))
    if missing:
        results = ask([GHIDRA_PY, str(Path(__file__).parent / program), str(LDEFS), language(arch, fp), *opts], missing)
        with db: db.executemany("insert or replace into results values (?, ?)", zip(missing, results))
        known.update(zip(missing, results))
    db.close()
    return [json.loads(known[line]) for line in lines]


class Session(threading.local):
    """This thread's one Ghidra-side program: its argv, process and stderr file. Per thread, not per
    process: emulate_sharded's threads sharing one interleaved its lines and closed it under each other."""
    argv = process = log = None


SESSION = Session()


def session(argv):
    """A long-lived Ghidra-side program: starting one (the JVM and the language) costs about 20 s of
    CPU and a worker answers many shards of one language. One per thread: each JVM takes 0.7-2 GB."""
    if SESSION.argv != argv or SESSION.process.poll() is not None:
        close()
        SESSION.argv, SESSION.log = argv, tempfile.TemporaryFile()
        # cwd="/" keeps repository modules from shadowing the venv's imports.
        SESSION.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=SESSION.log, text=True, cwd="/")
    return SESSION.process


def close(kill=False):
    """End this thread's program: end of input lets it finish, `kill` when it has failed."""
    if process := SESSION.process:
        if kill: process.kill()
        else: process.stdin.close()
        process.wait()
    SESSION.argv = SESSION.process = SESSION.log = None


def ask(argv, lines):
    """One result per line from the session for argv; other output (pyghidra's start-up, Ghidra's
    log) is skipped, and reported with stderr if the session fails."""
    process = session(argv)
    # A writer thread: the program answers while we are still sending, and both pipes are bounded.
    writer = threading.Thread(target=lambda: (process.stdin.write("".join(line + "\n" for line in lines)), process.stdin.flush()), daemon=True)
    writer.start()
    results, other = [], collections.deque(maxlen=50)
    while len(results) < len(lines) and (line := process.stdout.readline()):
        (results if line.startswith("{") else other).append(line.rstrip("\n"))
    writer.join()
    if len(results) != len(lines):
        log = SESSION.log; close(kill=True)
        log.seek(0); error = log.read().decode(errors="replace")[-3000:]
        raise RuntimeError(f"{Path(argv[1]).name} {argv[3]}: {len(results)}/{len(lines)} results\n" + "\n".join(other) + f"\n{error}")
    return results


def emulate(arch, fp, requests):
    return run("ghidra_emulate.py", arch, fp, [json.dumps(r) for r in requests]) if requests else []


def disassemble(arch, fp, words, *opts):
    """words: [(word bytes, address)]."""
    return run("ghidra_decode.py", arch, fp, [f"{a:x} {w.hex()}" for w, a in words], *opts) if words else []


def concurrently(fn, jobs):
    """{key: args} -> {key: fn(*args)}, one Ghidra process per job. Processes, not threads: jobs
    decode and compare in Python around their Ghidra call, which threads would serialize."""
    with ProcessPoolExecutor(jvm_workers(18, len(jobs))) as pool:
        futures = {key: pool.submit(fn, *args) for key, args in jobs.items()}
    return {key: f.result() for key, f in futures.items()}


def outputs(result):
    return [int.from_bytes(bytes.fromhex(x), "little") for x in result["outputs"]]


def emulate_once(arch, fp, requests):
    """`emulate` on a pool thread: its program ends with the job, not with the thread."""
    try: return emulate(arch, fp, requests)
    finally: close()


def emulate_sharded(batches, workers=None):
    """{key: (arch, fp, requests)} -> {key: results}, with every batch split into contiguous
    chunks of about equal size so `workers` processes stay busy (default: `jvm_workers`)."""
    workers = workers or jvm_workers()
    total = sum(len(r) for _, _, r in batches.values())
    size = max(1, -(-total // workers))
    jobs = {(key, i): (arch, fp, requests[i:i + size]) for key, (arch, fp, requests) in batches.items()
            for i in range(0, max(len(requests), 1), size)}
    with ThreadPoolExecutor(min(workers, len(jobs)) or 1) as pool:
        futures = {job: pool.submit(emulate_once, *args) for job, args in jobs.items()}
    return {key: [r for (k, i), f in sorted(futures.items(), key=lambda kv: kv[0][1]) if k == key for r in f.result()] for key in batches}
