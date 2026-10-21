"""Batch requests through the Ghidra-side programs, which run under the pyghidra venv. Results are
cached per language and program (see `stamp`), so reruns only send requests Ghidra hasn't answered."""
import ast, hashlib, json, os, struct, subprocess
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import cache as memoize
from pathlib import Path
from sass.paths import CACHE, GHIDRA_PY, LANGUAGES, LDEFS, ROOT, language
from sass.sqlcache import open_cache

SOURCES = ROOT / "src"


def module_file(module):
    p = SOURCES / module.replace(".", "/")
    return next((f for f in (p.with_suffix(".py"), p / "__init__.py") if f.exists()), None)


# Generator-only modules: what they produce reaches Ghidra only through the hashed .sla and manifest,
# and the Ghidra-side programs never load them (test_ghidra_programs_skip_generators).
GENERATORS = ("sass.semantics.families", "sass.semantics.operations", "sass.semantics.operands", "sass.sleigh.lower", "sass.sleigh.build")


def generator(module): return any(module == g or module.startswith(g + ".") for g in GENERATORS)


@memoize
def sources(program):
    """The sass sources `program` (a module of this package) can run: its transitive sass imports,
    function-level ones included, with their packages, and this package's Java. The programs run
    as scripts beside each other, so a bare import may name a sibling module."""
    seen, todo = set(), [f"{__name__}.{Path(program).stem}"]
    while todo:
        if (module := todo.pop()) in seen or generator(module) or not (f := module_file(module)): continue
        seen.add(module)
        parts = module.split(".")
        package = parts if f.name == "__init__.py" else parts[:-1]
        todo += [".".join(parts[:i]) for i in range(1, len(parts))]
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Import): todo += [x for a in n.names for x in (a.name, f"{__name__}.{a.name}")]
            elif isinstance(n, ast.ImportFrom):
                base = ".".join(package[:len(package) - n.level + 1] + [n.module or ""]).strip(".") if n.level else n.module
                todo += [base, f"{__name__}.{base}", *(f"{base}.{a.name}" for a in n.names)]
    return sorted({module_file(m) for m in seen} | set(Path(__file__).parent.glob("*.java")))


def stamp(program, arch, fp):
    """Hash of everything a Ghidra-side answer depends on: the compiled language and its specs,
    runtime manifest (or legacy coverage manifest), the sass sources the program runs and the
    Ghidra install."""
    from sass.sleigh.gen import variant
    name = variant(arch, fp)
    manifest = LANGUAGES / f"sass_{name}_runtime.json"
    if not manifest.exists(): manifest = LANGUAGES / f"sass_{name}_coverage.json"
    files = [LANGUAGES / f"sass_{name}.sla", manifest, LDEFS, LANGUAGES / "sass.pspec", LANGUAGES / "sass.cspec", *sources(program)]
    h = hashlib.sha256(GHIDRA_PY.encode() + os.environ.get("GHIDRA_INSTALL_DIR", "").encode())
    for f in files: h.update(str(f).encode()); h.update(f.read_bytes() if f.exists() else b"")
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
        # cwd="/" keeps repository modules from shadowing the venv's imports.
        p = subprocess.run([GHIDRA_PY, str(Path(__file__).parent / program), str(LDEFS), language(arch, fp), *opts],
                           input="".join(line + "\n" for line in missing), capture_output=True, text=True, cwd="/")
        results = [line for line in p.stdout.splitlines() if line.startswith("{")]
        if p.returncode or len(results) != len(missing):
            raise RuntimeError(f"{program} {language(arch, fp)}: {len(results)}/{len(missing)} results\n{p.stderr[-3000:]}")
        with db: db.executemany("insert or replace into results values (?, ?)", zip(missing, results))
        known.update(zip(missing, results))
    db.close()
    return [json.loads(known[line]) for line in lines]


def emulate(arch, fp, requests):
    return run("ghidra_emulate.py", arch, fp, [json.dumps(r) for r in requests]) if requests else []


# Launch constants in bank 0: (blockDim.xyz, local stack base, shared window, local window) offsets, from
# nvcc 13.0 probe kernels reading each value (2026-10-15; docs/testing.md). SM90+ build the generic shared
# base from SR_SWINHI alone (None). SM107 has no nvcc target to check.
LAUNCH_LAYOUT = {**dict.fromkeys(("SM75", "SM80", "SM86", "SM89"), (0x0, 0x28, 0x18, 0x20)), "SM90": (0x0, 0x28, None, 0x20),
                 **dict.fromkeys(("SM100", "SM101", "SM103", "SM120"), (0x360, 0x37c, None, 0x2f8))}
# The emulated generic windows (base, size): 4 GiB-aligned like SM90+'s shared base, 16 MiB like SM89's.
WINDOWS = dict(SWIN=(0x7f00_00000000, 1 << 24), LWIN=(0x7e00_00000000, 1 << 24))


def window_registers():
    """The SR_*WIN* registers describing WINDOWS, for every thread."""
    return {f"SR_{w}{part}": v for w, (base, size) in WINDOWS.items()
            for part, v in (("LO", base & 0xffffffff), ("HI", base >> 32), ("SZ", size))}


def kernel_request(image, arguments, memory, threads, block, output, cooperative=False, context=None):
    """The emulator's whole-kernel request for `image` (`sass.decode.cubin.kernel_image`) launched as
    `threads` threads in blocks of `block`: `arguments` are the kernel parameter values in order, packed
    into constant bank 0 with the launch dimensions, local stack base and generic windows (also in every
    thread's SR_*WIN* registers); `memory` adds regions
    [{space, address, hex}]; `output` is {address, size, stride}, read back per thread."""
    if image["arch"] not in LAUNCH_LAYOUT: raise ValueError(f"{image['arch']}: launch constant layout unknown (known: {', '.join(LAUNCH_LAYOUT)})")
    dims, stack, shared, local = LAUNCH_LAYOUT[image["arch"]]
    constants = dict(image["constants"]); bank = bytearray(constants.get(0, b""))
    bank.extend(bytes(max(0, image["parameter_base"] + image["parameter_size"] - len(bank))))
    struct.pack_into("<III", bank, dims, block, 1, 1)
    struct.pack_into("<I", bank, stack, 0x1000)
    for offset, window in ((shared, "SWIN"), (local, "LWIN")):
        if offset is not None: struct.pack_into("<Q", bank, offset, WINDOWS[window][0])
    for parameter, value in zip(image["parameters"], arguments):
        struct.pack_into("<Q" if parameter["size"] == 8 else "<I", bank, image["parameter_base"] + parameter["offset"], value)
    constants[0] = bytes(bank)
    return dict(kernel=dict(name=image["name"], code=image["code"].hex(), threads=threads, block_size=block, cooperative=cooperative,
                            registers=window_registers()),
                memory=[dict(space="cbank", address=index << 32, hex=value.hex()) for index, value in constants.items()] + memory,
                output=output, context=context or {})


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
