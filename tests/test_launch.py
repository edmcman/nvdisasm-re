import struct
from dataclasses import replace
import pytest
from sass import ghidra_bridge, launch
from sass.decode import cubin
from sass.emulate import reference, schedule
from tests.harness import arguments, gpu, hardware, launch_geometry
from tests.refs.launch import coordinates


SIZES = (1, 2, 3, 4, 8, 12, 16, 4096)


@pytest.mark.parametrize("value,want", [(7, (7, 1, 1)), ([2], (2, 1, 1)), ((2, 3), (2, 3, 1)), ([2, 3, 4], (2, 3, 4))])
def test_dimensions(value, want):
    assert launch.dimensions(value) == want


@pytest.mark.parametrize("value", [0, -1, 1 << 32, True, 1.5, "2", None, (), [1, 2, 3, 4], [1, 0], [1, True], [1, 2.0]])
def test_invalid_dimensions(value):
    with pytest.raises(ValueError, match="dimensions"): launch.dimensions(value)


def test_geometry_counts():
    assert launch.geometry(block=(2, 3), grid=[4, 5, 6]) == ((4, 5, 6), (2, 3, 1), 720)
    assert launch.geometry(35, 17) == ((3, 1, 1), (17, 1, 1), 35)
    assert launch.geometry((1 << 64) - 1, ((1 << 32) - 1, (1 << 32) - 1))[2] == (1 << 64) - 1
    for kw in (dict(), dict(threads=True), dict(threads=0), dict(threads=1 << 64),
               dict(threads=31, grid=1), dict(grid=(1 << 31, 1 << 31, 4), block=1),
               dict(threads=1, block=((1 << 32) - 1,) * 3)):
        with pytest.raises(ValueError): launch.geometry(**kw)


@pytest.mark.parametrize("arch", launch.LAUNCH_LAYOUT)
def test_geometry_constants(arch):
    image = dict(name="k", arch=arch, code=bytes(16), constants={}, parameter_base=0, parameter_size=0, parameters=[])
    request = launch.kernel_request(image, (), [], output={}, grid=(2, 3, 4), block=[5, 2, 3])
    state = request["kernel"]
    assert (state["grid"], state["block"], state["threads"], state["block_size"]) == ((2, 3, 4), (5, 2, 3), 720, 30)
    bank = bytes.fromhex(request["memory"][0]["hex"])
    assert struct.unpack_from("<III", bank, launch.LAUNCH_LAYOUT[arch][0]) == (5, 2, 3)
    assert struct.unpack_from("<III", bank, launch.GRID_LAYOUT[arch]) == (2, 3, 4)
    with pytest.raises(ValueError, match="does not match"):
        launch.kernel_request(image, (), [], threads=719, grid=(2, 3, 4), block=(5, 2, 3))
    with pytest.raises(ValueError, match="block_size"):
        schedule.launch(dict(state, block_size=31))
    legacy = launch.kernel_request(image, (), [], 35, 17, {})["kernel"]
    assert "grid" not in legacy and "block" not in legacy
    assert schedule.launch(dict(legacy, threads=1))[2:4] == (1, 17)
    assert schedule.seeds(legacy, 34, 17)["SR_LANEID"] == 0


@pytest.mark.parametrize("arch", ("SM89", "SM90"))
def test_geometry_reference(arch):
    for f in launch_geometry.fixtures(arch):
        result = reference.run_kernel(gpu.request(f), arch)
        assert not hardware.emulated_problems(f, result), (f.name, result)


@pytest.mark.parametrize("grid,block", [*launch_geometry.SHAPES, *(([2, 2, 2], b) for b in launch_geometry.PARTIAL_BLOCKS)])
def test_coordinate_seeds(grid, block):
    state = dict(grid=grid, block=block)
    names = [f"SR_{name}.{axis}" for name in ("TID", "CTAID") for axis in "XYZ"]
    for t, row in enumerate(coordinates(grid, block)):
        seeds = schedule.seeds(state, t, block[0] * block[1] * block[2])
        assert [seeds[name] for name in names] == list(row[:6])
        assert seeds["SR_LANEID"] == row[-1]


def test_implicit_warpsync_all():
    from sass.decode import pydecode
    d = pydecode.decode("SM90", gpu.encode("warpsync__RIR", arch="SM90").to_bytes(16, "little"))
    assert "all" not in d.env
    assert schedule.sync_form("SM90", d) == ("all",)


def test_legacy_incomplete_geometry():
    f = next(f for f in launch_geometry.fixtures("SM89") if f.name == "geometry_0_tid_y")
    request = gpu.request(f)
    request["kernel"].pop("grid")
    request["kernel"]["threads"] -= 2
    result = reference.run_kernel(request, "SM89")
    assert result["outputs"] == [v.to_bytes(4, "little").hex() for v in f.expected[:-2]]


@pytest.mark.parametrize("block", [1, 17, 33, 45])
def test_fixture_partial_blocks(block):
    f = gpu.Fixture("partial", b"", "k", bytes(12 * 2 * block), [0] * 2 * block, block=block)
    assert gpu.fixture_geometry(f) == ((2, 1, 1), (block, 1, 1), 2 * block)


@pytest.mark.parametrize("changes", [dict(block=17), dict(grid=(2, 2), block=8), dict(block=0),
                                   dict(data=bytes(13)), dict(expected=[0])])
def test_gpu_geometry_before_resources(monkeypatch, changes):
    image = dict(arch="SM89", parameters=[dict(size=8), dict(size=8)])
    monkeypatch.setattr(cubin, "kernel_image", lambda *_: image)
    driver = object.__new__(gpu.Driver); driver.arch = "SM89"
    f = replace(gpu.Fixture("invalid", b"", "k", bytes(12 * 64), [0] * 64), **changes)
    with pytest.raises(ValueError): driver.run(f)


def test_fixture_geometry_fingerprint(monkeypatch):
    monkeypatch.setattr(cubin, "kernel_image", lambda *_: {})
    f = gpu.Fixture("k", b"", "k", bytes(12 * 32), [0] * 32)
    import hashlib
    old = ({}, f.kernel, f.input_size, f.output_size, f.arguments, f.block, f.expected)
    assert f.fingerprint == hashlib.sha256(f.data + repr(old).encode()).hexdigest()[:16]
    assert f.fingerprint != replace(f, grid=1).fingerprint


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("boundary", ("zero", "maximum"))
def test_unsigned_arguments(size, boundary):
    value = 0 if boundary == "zero" else (1 << (8 * size)) - 1
    assert launch.serialize_arguments([size], [value]) == [bytes([0 if boundary == "zero" else 255]) * size]


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("kind", (bytes, bytearray, memoryview))
def test_raw_arguments(size, kind):
    payload = bytes(i % 256 for i in range(size))
    assert launch.serialize_arguments([size], [kind(payload)]) == [payload]


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("invalid", ("negative", "overflow", "short", "long", "float", "string", "none"))
def test_invalid_arguments(size, invalid):
    value = {"negative": -1, "overflow": 1 << (8 * size), "short": bytes(size - 1),
             "long": bytes(size + 1), "float": 1.0, "string": "a", "none": None}[invalid]
    error = TypeError if invalid in ("float", "string", "none") else ValueError
    with pytest.raises(error, match=rf"argument 1 \({size} bytes\)"):
        launch.serialize_arguments([4, size], [0, value])


@pytest.mark.parametrize("sizes,values,message", [
    ([4, 12], [0], r"argument 1 \(12 bytes\): missing"),
    ([4], [0, 1], r"argument 1 \(no declared size\): extra"),
    ([], [0], r"argument 0 \(no declared size\): extra"),
])
def test_argument_counts(sizes, values, message):
    with pytest.raises(ValueError, match=message):
        launch.serialize_arguments(sizes, values)


def test_empty_and_existing_arguments():
    assert launch.serialize_arguments([], []) == []
    assert launch.serialize_arguments(iter([4, 8]), iter([0xabcdef01, 0x123456789abcdef0])) == [
        struct.pack("<I", 0xabcdef01), struct.pack("<Q", 0x123456789abcdef0)]
    assert launch.serialize_arguments([4, 4], [struct.pack("<i", -7), struct.pack("<f", 1.5)]) == [
        b"\xf9\xff\xff\xff", b"\0\0\xc0?"]


def test_mixed_arguments_and_memoryview_byte_size():
    assert launch.serialize_arguments([1, 2, 3, 4, 8, 12, 16],
                                      [0x12, 0x3456, 0x789abc, bytearray(b"\0abc"),
                                       0x123456789abcdef0, bytes(range(12)), memoryview(bytes(range(16)))]) == [
        b"\x12", b"\x56\x34", b"\xbc\x9a\x78", b"\0abc",
        b"\xf0\xde\xbc\x9a\x78\x56\x34\x12", bytes(range(12)), bytes(range(16))]
    payload = memoryview(b"\x01\0\x02\0").cast("H")
    assert len(payload) == 2
    assert launch.serialize_arguments([4], [payload]) == [b"\x01\0\x02\0"]
    assert launch.serialize_arguments([2], [memoryview(b"\x01\0\x02\0")[::2]]) == [b"\x01\x02"]


def test_parameter_offsets_preserve_padding_and_constants():
    sizes = SIZES[:-1]
    offsets = (0, 2, 5, 12, 24, 40, 64)
    parameters = [dict(size=size, offset=offset) for size, offset in zip(sizes, offsets)]
    bank = bytes([0xa5]) * 0x200
    image = dict(name="k", arch="SM89", code=b"", constants={0: bank, 2: b"other"},
                 parameter_base=0x160, parameter_size=80, parameters=parameters)
    payloads = [bytes(range(size)) for size in sizes]
    expected = bytearray(bank)
    for offset, payload in zip(offsets, payloads):
        expected[0x160 + offset:0x160 + offset + len(payload)] = payload
    result = launch.kernel_request(image, payloads, [], 32, 32, {})
    constants = {entry["address"]: bytes.fromhex(entry["hex"]) for entry in result["memory"]}
    assert constants[0][0x30:] == expected[0x30:]
    assert len(constants[0]) == len(bank)
    assert constants[2 << 32] == b"other"
    assert image["constants"][0] == bank
    with pytest.raises(ValueError, match="missing"):
        launch.kernel_request(image, payloads[:-1], [], 32, 32, {})
    with pytest.raises(ValueError, match="extra"):
        launch.kernel_request(image, payloads + [0], [], 32, 32, {})


def test_parameter_bank_extension():
    image = dict(name="k", arch="SM89", code=b"", constants={0: bytes(0x160)},
                 parameter_base=0x160, parameter_size=4096, parameters=[dict(offset=0, size=4096)])
    payload = bytes(range(256)) * 16
    result = launch.kernel_request(image, [payload], [], 32, 32, {})
    assert bytes.fromhex(result["memory"][0]["hex"])[0x160:] == payload


@pytest.mark.parametrize("declared,value,actual,error", [
    (1, -1, 1, "unsigned integer"),
    (1, 256, 1, "unsigned integer"),
    (12, b"short", 12, "payload"),
    (4, 0, 2, "fixture declares"),
    (1, 0, None, "extra"),
])
def test_gpu_arguments_validated_before_resources(monkeypatch, declared, value, actual, error):
    image = dict(arch="SM89", parameters=[dict(size=8), dict(size=8)] +
                 ([] if actual is None else [dict(size=actual)]))
    monkeypatch.setattr(cubin, "kernel_image", lambda *_: image)
    driver = object.__new__(gpu.Driver)
    driver.arch = "SM89"
    fixture = gpu.Fixture("invalid", b"", "k", bytes(384), [0] * 32, arguments=((declared, value),))
    with pytest.raises(ValueError, match=error):
        driver.run(fixture)


def test_gpu_validates_actual_image(monkeypatch):
    images = {b"hardware": dict(arch="SM89", parameters=[dict(size=8), dict(size=8), dict(size=12)]),
              b"emulator": dict(arch="SM89", parameters=[dict(size=8), dict(size=8), dict(size=4)])}
    monkeypatch.setattr(cubin, "kernel_image", lambda image, _: images[image])
    driver = object.__new__(gpu.Driver)
    driver.arch = "SM89"
    fixture = gpu.Fixture("invalid", b"hardware", "k", bytes(384), [0] * 32,
                          arguments=((4, 1),), emulator_image=b"emulator")
    with pytest.raises(ValueError, match=r"argument 2 \(12 bytes\): fixture declares 4"):
        driver.run(fixture)


@pytest.mark.parametrize("value,error", [(None, ValueError), (1.5, TypeError)])
def test_gpu_missing_or_unsupported_arguments(monkeypatch, value, error):
    image = dict(arch="SM89", parameters=[dict(size=8), dict(size=8), dict(size=16)])
    monkeypatch.setattr(cubin, "kernel_image", lambda *_: image)
    driver = object.__new__(gpu.Driver)
    driver.arch = "SM89"
    fixture = gpu.Fixture("invalid", b"", "k", bytes(384), [0] * 32,
                          arguments=() if value is None else ((16, value),))
    with pytest.raises(error, match=r"argument 2 \(16 bytes\)"):
        driver.run(fixture)


@pytest.mark.parametrize("arch", ("SM89", "SM90"))
def test_aggregate_kernel_reference(arch):
    fixture = arguments.fixture(arch)
    image = cubin.kernel_image(fixture.image, fixture.kernel)
    assert [p["size"] for p in image["parameters"]] == [8, 8, *SIZES[:-1]]
    result = reference.run_kernel(gpu.request(fixture), arch)
    assert not hardware.emulated_problems(fixture, result), result


@pytest.mark.gpu
def test_aggregate_kernel_gpu(request):
    arch = gpu.target(request.config)
    if arch is None:
        pytest.skip(request.config._gpu_unavailable)
    fixture = arguments.fixture(arch)
    result = reference.run_kernel(gpu.request(fixture), arch)
    assert not hardware.emulated_problems(fixture, result), result
    assert not hardware.run([replace(fixture, expected=ghidra_bridge.outputs(result))])[fixture.name]
