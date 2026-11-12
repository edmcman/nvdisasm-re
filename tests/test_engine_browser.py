"""Exercise the shipped Java filesystem through Ghidra discovery, not a parallel extractor."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess

import pytest

from sass.assemble import assemble_cubin
from sass.paths import GHIDRA_PY, LDEFS, NVDISASM


def engine(payload, metadata=None):
    raw = struct.pack("<IIQQ", 0x74727466, 0, 2, 24 + len(payload)) + payload
    if metadata is None:
        return raw
    return struct.pack("<I", len(metadata)) + metadata + raw


def literal_lz4(data):
    """A valid raw LZ4 block consisting only of its final literal sequence."""
    count = len(data)
    result = bytearray([min(count, 15) << 4])
    if count >= 15:
        count -= 15
        while count >= 255:
            result.append(255)
            count -= 255
        result.append(count)
    return bytes(result) + data


def record(data, *, compressed=False, kind=2, version=0x101, flags=None, decoded=None, arch=89):
    payload = literal_lz4(data) if compressed else data
    stored = (len(payload) + 7) & ~7
    header = struct.pack("<HHIQQIIQQQQ", kind, version, 64, stored,
                         len(payload) if compressed else 0, 0x10007, arch, 0,
                         (0x2011 if compressed else 0x11) if flags is None else flags,
                         0, len(data) if decoded is None else decoded)
    return header + payload + bytes(stored - len(payload))


def fatbin(*records, version=1):
    payload = b"".join(records)
    return struct.pack("<IHHQ", 0xBA55ED50, version, 16, len(payload)) + payload


def change(data, offset, fmt, value):
    result = bytearray(data)
    struct.pack_into(fmt, result, offset, value)
    return bytes(result)


@pytest.mark.ghidra
def test_engine_browser(tmp_path, request):
    from tests.harness.select import arches, modes, require_languages
    if "SM89" not in arches(request.config) or "exact" not in modes(request.config):
        pytest.skip("SM89 exact not selected")
    require_languages([("SM89", "exact")])
    cubin = assemble_cubin(""".target SM89
.kernel browser_kernel
.param input, 4, 4
.param output, 8, 8
.registers 8
MOV R0, c[0][$input];
EXIT;
.endkernel
""")
    other = assemble_cubin(""".target SM89
.kernel another_kernel
.registers 8
EXIT;
.endkernel
""")
    metadata = b'{"description":"fixture","author":"Ultralytics","task":"detect"}'
    legacy = change(change(change(cubin, 8, "<B", 7), 20, "<I", 0x81),
                    48, "<I", 0x590559)
    modules = {hashlib.sha256(data).hexdigest(): data for data in (cubin, other, legacy)}
    for sha, data in modules.items():
        (tmp_path / f"{sha}.cubin").write_bytes(data)
    fixtures = []

    def add(name, payload, occurrences=0, unique=0, diagnostic=None, *, wrapped=False,
            accepted=True, encoded=False):
        path = tmp_path / f"{name}.engine"
        path.write_bytes(payload if encoded else engine(payload, metadata if wrapped else None))
        fixtures.append(dict(name=name, file=str(path), occurrences=occurrences, unique=unique,
                             diagnostic=diagnostic, wrapped=wrapped, accepted=accepted))

    add("raw", cubin, 1, 1)
    add("wrapped", cubin, 1, 1, wrapped=True)
    add("compressed", fatbin(record(cubin, compressed=True)), 1, 1)
    add("uncompressed", fatbin(record(cubin)), 1, 1)
    add("legacy", legacy, 1, 1)
    add("legacy_compressed", fatbin(record(legacy, compressed=True)), 1, 1)
    add("multiple", cubin + b"noise" + fatbin(record(other), record(cubin, compressed=True))
        + b"noise" + cubin, 4, 2, wrapped=True)
    # Magic crosses the parser's scan buffer boundary.
    add("boundary", bytes(65534) + cubin, 1, 1)
    add("empty", b"")
    add("truncated_elf", cubin[:63], diagnostic="ELF range outside object")
    add("wrong_machine", change(cubin, 18, "<H", 62), diagnostic="Not a little-endian CUDA ELF")
    add("section_offset_overflow", change(cubin, 40, "<Q", (1 << 64) - 1),
        diagnostic="ELF range outside object")
    add("section_count", change(cubin, 60, "<H", 65535), diagnostic="ELF range outside object")
    shoff = struct.unpack_from("<Q", cubin, 40)[0]
    add("section_size_overflow", change(cubin, shoff + 64 + 32, "<Q", (1 << 64) - 1),
        diagnostic="Invalid ELF section")
    add("bad_name", change(cubin, shoff + 64, "<I", 0xffffffff),
        diagnostic="Invalid ELF name offset")
    add("bad_name_table", change(cubin, shoff + 64 + 4, "<I", 1),
        diagnostic="Invalid ELF name table")
    add("bad_symbol_link", change(cubin, shoff + 3 * 64 + 40, "<I", 0),
        diagnostic="Invalid ELF symbol table")
    add("bad_symbol_entry_size", change(cubin, shoff + 3 * 64 + 56, "<Q", 1),
        diagnostic="Invalid ELF symbol table")
    add("bad_phoff", change(cubin, 32, "<Q", (1 << 64) - 1),
        diagnostic="ELF range outside object")
    add("bad_phsize", change(cubin, 54, "<H", 1),
        diagnostic="Invalid or unsupported ELF table header")
    # Rejected fatbins' payloads must never reappear as raw cubins.
    add("unsupported_fatbin", fatbin(record(cubin), version=2),
        diagnostic="Unsupported fatbin version")
    add("unsupported_record", fatbin(record(cubin, kind=1)), diagnostic="Unsupported record")
    add("unsupported_flags", fatbin(record(cubin, flags=0x4000)),
        diagnostic="Unsupported fatbin flags")
    add("unsupported_then_valid", fatbin(record(cubin, kind=1), record(other)), 1, 1,
        diagnostic="Unsupported record")
    add("wrong_arch", fatbin(record(cubin, arch=75)),
        diagnostic="Fatbin and ELF architectures disagree")
    add("truncated_fatbin", fatbin(record(cubin))[:-1], diagnostic="Invalid or truncated fatbin size")
    add("truncated_record", fatbin(b"\x02"), diagnostic="Truncated record header")
    add("invalid_record_size", fatbin(change(record(cubin), 8, "<Q", (1 << 64) - 1)),
        diagnostic="Invalid or truncated record size")
    add("invalid_compressed_size", fatbin(change(record(cubin, compressed=True), 16, "<Q", 0)),
        diagnostic="Invalid compressed or decoded size")
    add("corrupt_compression", fatbin(change(record(cubin, compressed=True), 64, "<B", 0)),
        diagnostic="LZ4")
    add("short_decoded", fatbin(record(cubin, compressed=True, decoded=len(cubin) - 1)),
        diagnostic="LZ4 output or input size mismatch")
    add("long_decoded", fatbin(record(cubin, compressed=True, decoded=len(cubin) + 1)),
        diagnostic="Truncated LZ4 output")
    add("object_limit", fatbin(record(cubin, compressed=True, decoded=(128 << 20) + 1)),
        diagnostic="128 MiB object limit exceeded")
    # Failed decodes still consume the total allowance, without allocating 512 MiB.
    add("total_limit", fatbin(*[record(cubin, compressed=True, decoded=128 << 20)] * 5),
        diagnostic="512 MiB total limit exceeded")
    add("truncated_engine", change(engine(cubin), 16, "<Q", 100000), 1, 1,
        diagnostic="Declared engine size", encoded=True)
    add("trailing_engine_data", engine(cubin) + other, 1, 1,
        diagnostic="Declared engine size", encoded=True)
    for name, prefix in (
        ("invalid_json", b'{"author":"Ultralytics"'),
        ("lenient_json", b"{'author':'Ultralytics','description':'fixture'}"),
        ("trailing_json", metadata + b" true"),
        ("invalid_utf8", metadata[:-1] + b'\xff}'),
        ("non_ultralytics", b'{"author":"other","description":"fixture"}'),
        ("deep_json", b'{"author":"Ultralytics","description":"fixture","x":' + b"[" * 70
         + b"0" + b"]" * 70 + b"}"),
    ):
        add(name, engine(cubin, prefix), accepted=False, encoded=True)
    add("bad_engine", change(engine(cubin), 0, "<I", 0), accepted=False, encoded=True)
    add("short_header", b"ftrt", accepted=False, encoded=True)
    add("json_only", struct.pack("<I", len(metadata)) + metadata, accepted=False, encoded=True)
    config = tmp_path / "fixtures.json"
    config.write_text(json.dumps(dict(fixtures=fixtures, modules=list(modules),
                                     ldefs=str(LDEFS), directory=str(tmp_path))))
    script = Path(__file__).parent / "harness/ghidra_engine_browser.py"
    process = subprocess.run([GHIDRA_PY, str(script), str(config)],
                             capture_output=True, text=True, timeout=120)
    assert process.returncode == 0, process.stdout + process.stderr
    report = json.loads(next(line for line in process.stdout.splitlines() if line.startswith("{")))
    assert report["fixtures"] == len(fixtures)
    assert report["imported"] == 4
    assert report["cancelled"]


@pytest.mark.ghidra
def test_supplied_engine(tmp_path):
    """Opt in without storing a downloaded engine in the repository."""
    path = os.environ.get("SASS_ENGINE_SAMPLE")
    if not path:
        pytest.skip("set SASS_ENGINE_SAMPLE to the supplied YOLO engine")
    from tests.harness.select import require_languages
    require_languages([("SM75", "exact")])
    config = tmp_path / "sample.json"
    config.write_text(json.dumps(dict(sample=path, directory=str(tmp_path), ldefs=str(LDEFS))))
    script = Path(__file__).parent / "harness/ghidra_engine_browser.py"
    process = subprocess.run([GHIDRA_PY, str(script), str(config)],
                             capture_output=True, text=True, timeout=120)
    assert process.returncode == 0, process.stdout + process.stderr
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert len(manifest["occurrences"]) == 109
    assert len(manifest["modules"]) == 65
    assert sum(len(module["elf"]["kernels"]) for module in manifest["modules"]) == 73
    assert not manifest["diagnostics"], manifest["diagnostics"]
    imports = json.loads((tmp_path / "imports.json").read_text())
    assert [entry["architecture"] for entry in imports] == ["SM75", "SM75"]
    assert all(any(entry["parameters"]) for entry in imports)
    for module in manifest["modules"]:
        cubin = tmp_path / module["path"]
        assert hashlib.sha256(cubin.read_bytes()).hexdigest() == module["sha256"]
        result = subprocess.run([NVDISASM, str(cubin)], capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
