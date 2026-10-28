"""Constant-bank labels follow established architecture ABI layouts."""
import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree
import pytest
from sass.paths import ROOT, GHIDRA_PY, LDEFS, language
from sass.sleigh import gen


@pytest.fixture
def languages(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "OUT", tmp_path)
    (tmp_path / "sass.pspec").write_bytes((ROOT / "processor/SASS/data/languages/sass.pspec").read_bytes())
    return tmp_path


@pytest.mark.parametrize("arch,dims,args,stack,shared,local,descriptor", [
    ("SM75", 0, 0x160, 0x28, 0x18, 0x20, None),
    ("SM80", 0, 0x160, 0x28, 0x18, 0x20, 0x118),
    ("SM86", 0, 0x160, 0x28, 0x18, 0x20, 0x118),
    ("SM89", 0, 0x160, 0x28, 0x18, 0x20, 0x118),
    ("SM90", 0, 0x210, 0x28, None, 0x20, 0x208),
    ("SM100", 0x360, 0x380, 0x37c, None, 0x2f8, 0x358),
    ("SM101", 0x360, 0x380, 0x37c, None, 0x2f8, 0x358),
    ("SM103", 0x360, 0x380, 0x37c, None, 0x2f8, 0x358),
    ("SM120", 0x360, 0x380, 0x37c, None, 0x2f8, 0x358),
    ("SM107", None, None, None, None, None, None),
])
def test_architecture_labels(languages, arch, dims, args, stack, shared, local, descriptor):
    for suffix in ("", "_readable"):
        (languages / f"sass_{arch.lower()}{suffix}.slaspec").touch()
    gen.write_ldefs()
    entries = ElementTree.parse(languages / "sass.ldefs").getroot()
    specs = {entry.get("processorspec") for entry in entries}
    assert specs == {f"sass_{arch.lower()}.pspec"}
    root = ElementTree.parse(languages / specs.pop()).getroot()
    assert root.find("programcounter").get("register") == "PC"
    symbols = {s.get("name"): (s.get("address"), int(s.get("size")))
               for s in root.findall("default_symbols/symbol")}
    expected = {}
    if dims is not None:
        expected.update({f"blockDim_{axis}": (f"cbank:{dims + i * 4:#x}", 4)
                         for i, axis in enumerate("xyz")})
    for name, offset, size in (("kernel_arguments", args, 4096), ("local_stack_base", stack, 4),
                              ("shared_window_base", shared, 8), ("local_window_base", local, 8),
                              ("default_global_descriptor", descriptor, 8)):
        if offset is not None: expected[name] = (f"cbank:{offset:#x}", size)
    assert symbols == expected
    block = root.find("default_memory_blocks/memory_block")
    if expected:
        assert block.get("start_address") == "cbank:0x0"
        assert int(block.get("length"), 0) == 65536
        assert block.get("initialized") == "false"
        assert block.get("mode") == "r"
        assert all(s.get("entry") == "false" for s in root.findall("default_symbols/symbol"))
    else:
        assert block is None
    paths = [languages / "sass.ldefs", languages / f"sass_{arch.lower()}.pspec"]
    mtimes = [p.stat().st_mtime_ns for p in paths]
    gen.write_ldefs()
    assert [p.stat().st_mtime_ns for p in paths] == mtimes


@pytest.mark.ghidra
@pytest.mark.parametrize("arch,dims", [("SM80", 0), ("SM89", 0), ("SM90", 0), ("SM100", 0x360)])
@pytest.mark.parametrize("fp", ["exact", "readable"])
@pytest.mark.parametrize("arguments", [False, True], ids=["labels", "arguments"])
def test_imported_constant_labels(tmp_path, request, arch, dims, fp, arguments):
    from sass.assemble import assemble_cubin
    from tests.harness.select import arches, modes, require_languages
    if arch not in arches(request.config) or fp not in modes(request.config):
        pytest.skip("architecture/lowering not selected")
    require_languages([(arch, fp)])
    load = "LDC" if arch == "SM100" else "MOV"
    uniform_load = "LDCU" if arch == "SM100" else "ULDC"
    descriptor = 0x358 if arch == "SM100" else 0x208 if arch == "SM90" else 0x118
    store = "STG.E [R2.64], R0" if arch in ("SM80", "SM89") else "STG.E desc[UR4][R2.64], R0"
    data = assemble_cubin(f""".target {arch}
.kernel constant_labels
.param output, 8, 8
.registers 8, 8
{load} R0, c[0][{dims:#x}];
{load} R2, c[0][$output];
{load} R3, c[0][$output+4];
{uniform_load}.64 UR4, c[0][{descriptor:#x}];
{store};
EXIT;
.endkernel
.kernel other_layout
.param value, 4, 4
.param output, 8, 8
.registers 8, 8
{load} R0, c[0][$value];
{load} R2, c[0][$output];
{load} R3, c[0][$output+4];
{load} R4, c[0][$value+0x10];
{uniform_load}.64 UR4, c[0][{descriptor:#x}];
{store};
EXIT;
.endkernel
""")
    cubin = tmp_path / "constant_labels.cubin"
    cubin.write_bytes(data)
    script = Path(__file__).parent / "harness/ghidra_constant_labels.py"
    result = subprocess.run([GHIDRA_PY, str(script), str(LDEFS), language(arch, fp), str(cubin),
                             *(["--arguments"] if arguments else [])],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))
    assert output["labels"]["blockDim_x"] == dims
    if arguments:
        assert output["parameters"] == [8]
        assert output["other_parameters"] == [4, 8]
