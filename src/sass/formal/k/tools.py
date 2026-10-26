"""Optional K toolchain integration; importing the exporter does not need K."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from sass import paths
from sass.formal import k


class ToolError(RuntimeError):
    pass


def available():
    return bool(os.environ.get("SASS_K_CONTAINER") or
                (os.environ.get("SASS_K_BIN") and
                 (Path(os.environ["SASS_K_BIN"]) / "kompile").is_file()) or
                shutil.which("kompile"))


def command(name, *args):
    container = os.environ.get("SASS_K_CONTAINER")
    if container:
        # Mirror absolute paths so generated files have the same names inside/outside.
        directories = {Path.cwd().resolve(), k.DEFINITION.parent}
        for arg in args:
            path = Path(str(arg))
            if path.is_absolute():
                directories.add(path if path.is_dir() else path.parent)
        mounts = []
        for path in sorted(directories):
            mounts += ["--mount", f"type=bind,src={path},dst={path}"]
        return ["docker", "run", "--rm", "--network=none",
                "--user", f"{os.getuid()}:{os.getgid()}", *mounts,
                "--workdir", str(Path.cwd()), container, "nice", name, *map(str, args)]
    binary = str(Path(os.environ["SASS_K_BIN"]) / name) if "SASS_K_BIN" in os.environ else shutil.which(name)
    if not binary: raise ToolError(f"{name} not found; see docs/semantics.md, K backend")
    return ["nice", binary, *map(str, args)]


def invoke(name, *args, timeout=120, check=True):
    try:
        result = subprocess.run(command(name, *args), text=True, capture_output=True,
                                timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ToolError(f"{name}: {error}") from error
    if check and result.returncode:
        raise ToolError(f"{name} exited {result.returncode}:\n{result.stderr}\n{result.stdout}")
    return result


def compile(backend="llvm"):
    """Compile once per definition content/toolchain, with serialized cache writes."""
    if backend not in ("llvm", "haskell"): raise ValueError("backend must be llvm or haskell")
    version = invoke("kompile", "--version").stdout
    if f"v{k.VERSION}" not in version and f" {k.VERSION}" not in version:
        raise ToolError(f"expected K {k.VERSION}, got {version.strip()}")
    digest = hashlib.sha256(version.encode())
    for source in k.SOURCES:
        digest.update(source.name.encode())
        digest.update(source.read_bytes())
    root = paths.CACHE / "formal/k" / digest.hexdigest()[:20]
    root.mkdir(parents=True, exist_ok=True)
    output = root / backend
    with (root / "build.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (output / "sass.ready").exists():
            invoke("kompile", k.DEFINITION, "--main-module", "SASS", "--syntax-module", "SASS",
                   "--backend", backend, "--output-definition", output, timeout=300)
            (output / "sass.ready").touch()
    return output


def execute(term, *, backend="llvm", timeout=120, structured=True):
    """Execute a K input term, returning the final KAST cells.

    A nonempty final computation is a stuck model and is reported as an error.
    Runtime responses and any proof contracts remain explicit inputs.
    """
    definition = compile(backend)
    root = definition.parent
    # Unique input names allow concurrent krun processes sharing a compiled definition.
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sass", dir=root, delete=False) as stream:
        if structured:
            from sass.formal.k import kast
            json.dump(kast.encode(term), stream)
        else: stream.write(term)
        filename = Path(stream.name)
    try:
        if structured:
            converted = invoke("kast", filename, "--input", "json", "--output", "kore",
                               "--definition", definition, timeout=timeout)
            filename.write_text(converted.stdout)
        parser = ["--parser", "cat"] if structured else []
        output = invoke("krun", filename, "--definition", definition, "--output", "json",
                        *parser, timeout=timeout)
    finally:
        filename.unlink()
    document = json.loads(output.stdout)
    term = document.get("term", document)
    cells = {}

    pending = [term]
    while pending:
        node = pending.pop()
        if not isinstance(node, dict): continue
        label = name(node.get("label", ""))
        if label.startswith("<") and label.endswith(">") and len(node.get("args", [])) == 1:
            cells[label[1:-1]] = decode(node["args"][0])
            continue
        pending.extend(node.get("args", []))
    if cells.get("k") not in ([], None):
        raise ToolError(f"K execution stuck: {cells['k']!r}")
    return cells


def prove(specification, module, *, timeout=120, check=True):
    """Prove an explicit claims module, with no implicit primitive-contract imports."""
    definition = compile("haskell")
    return invoke("kprove", Path(specification).resolve(), "--definition", definition,
                  "--spec-module", module, "-I", k.DEFINITION.parent,
                  timeout=timeout, check=check)


def decode(node):
    """Decode without Python recursion or quadratic associative-list concatenation."""
    results, pending = {}, [(node, False, None)]
    associative = {"_Map_", "_Map__MAP", "_List_", "_List__LIST"}
    while pending:
        current, ready, children = pending.pop()
        kind, ident = current["node"], id(current)
        if kind == "KToken":
            sort, value = name(current["sort"]), current["token"]
            results[ident] = int(value) if sort == "Int" else (
                json.loads(value) if sort == "String" else value == "true" if sort == "Bool" else value)
            continue
        if kind not in ("KApply", "KSequence"):
            results[ident] = current
            continue
        label = name(current.get("label", ""))
        if not ready:
            children = list(current.get("items", current.get("args", [])))
            if label in associative:
                flattened, todo = [], list(reversed(children))
                while todo:
                    child = todo.pop()
                    if child.get("node") == "KApply" and name(child["label"]) == label:
                        todo.extend(reversed(child["args"]))
                    else: flattened.append(child)
                children = flattened
            pending.append((current, True, children))
            pending.extend((child, False, None) for child in reversed(children))
            continue
        args = [results[id(child)] for child in children]
        if kind == "KSequence": result = args
        elif label.startswith(".Map"): result = {}
        elif label.startswith(".List") or label == ".K": result = []
        elif label in ("_Map_", "_Map__MAP"):
            result = {key: value for part in args for key, value in part.items()}
        elif label in ("_|->_", "_|->_MAP"): result = {args[0]: args[1]}
        elif label in ("_List_", "_List__LIST"): result = [item for part in args for item in part]
        elif label.startswith("ListItem"): result = args
        elif label.startswith("location(") or label.startswith("byte("): result = tuple(args)
        else: result = (label, *args)
        results[ident] = result
    return results[id(node)]


def name(value):
    """KAST v4 labels/sorts are structured; v3 uses strings."""
    return value["name"] if isinstance(value, dict) else value
