"""The C++ runtime primitives (sass/emulate/include/sass/primitives.hpp) compute the per-lane
references (refs/primitive.py) for every lane of random and edge-case partially active warps."""
import random
import pytest
from sass.emulate import native
from tests.refs import primitive

MASKS = (0xffffffff, 1, 1 << 31, 0x80000001, 0x55555555, 0x0000ffff)
TRIALS = 40
REDUCTIONS = ("and", "or", "xor", "add", "umin", "umax", "smin", "smax")


def warps(seed, width=32):
    """(values, active) pairs: edge masks and random ones, values with repeats (so matches happen)."""
    rng = random.Random(seed)
    for t in range(TRIALS):
        active = MASKS[t] if t < len(MASKS) else rng.getrandbits(32) or 1
        pool = [rng.getrandbits(width) for _ in range(rng.choice((1, 3, 32)))]
        yield [rng.choice(pool) for _ in range(32)], active


def each_lane(active): return primitive.lanes(active)


def test_read_lane():
    for values, active in warps(1):
        for lane in each_lane(active):
            src = random.Random(lane).randrange(32)
            assert native.primitive("read_lane", [values, src], lane, active) == primitive.read_lane(values, src)


def test_read_lane_out_of_range_fails():
    with pytest.raises(native.Failure, match="out of range"): native.primitive("read_lane", [[0] * 32, 32])


def test_ballot():
    for values, active in warps(2, 1):
        for lane in each_lane(active):
            assert native.primitive("ballot", [values], lane, active) == primitive.ballot(values, active)


@pytest.mark.parametrize("width", (32, 64))
def test_match(width):
    for values, active in warps(3, width):
        for lane in each_lane(active):
            assert native.primitive("match", [values], lane, active) == primitive.match(values, active, lane)


@pytest.mark.parametrize("op", REDUCTIONS)
def test_redux(op):
    for values, active in warps(4):
        lane = each_lane(active)[0]
        assert native.primitive(f"redux_{op}", [values], lane, active) == primitive.redux(op, values, active)


def test_address_space():
    rng = random.Random(5)
    ranges = [("global", 0, 1 << 40), ("shared", 0x7f00_00000000, 0x7f00_01000000), ("shared_cluster", 0x7f00_00000000, 0x7f00_04000000),
              ("local", 0x7e00_00000000, 0x7e00_01000000), ("global", 0xffff_ffff_0000_0000, 1 << 64)]
    probes = [0, 1 << 40, (1 << 40) - 1, 0x7f00_00000000, 0x7f00_01000000, 0x7f00_03ffffff, 0x7e00_00ffffff, (1 << 64) - 1,
              *(rng.getrandbits(64) for _ in range(200)), *(rng.randrange(0x7e00_00000000, 0x7f00_05000000) for _ in range(200))]
    for address in probes:
        want = primitive.address_space(address, ranges)
        if want is None:
            with pytest.raises(native.Failure, match="no unique mapped space"): native.primitive("address_space", [address], ranges=ranges)
        else: assert native.primitive("address_space", [address], ranges=ranges) == want, hex(address)


def test_unknown_primitive_fails():
    with pytest.raises(native.Failure, match="primitive frobnicate"): native.primitive("frobnicate", [0])


def test_memory_descriptor():
    native.memory_descriptor(0x1234)
    with pytest.raises(native.Failure, match="range-form memory descriptor 0x800000000000000"): native.memory_descriptor(1 << 59)
