import struct
from math import prod
from tests.harness import gpu
from tests.refs import launch


SHAPES = (((2, 3, 1), (3, 2, 1)), ((2, 1, 3), (2, 3, 2)), ((1, 2, 2), (1, 1, 3)))
PARTIAL_BLOCKS = ((1, 1, 1), (17, 1, 1), (3, 11, 1), (3, 3, 5))
FIELDS = ("tid_x", "tid_y", "tid_z", "ctaid_x", "ctaid_y", "ctaid_z",
          "block_x", "block_y", "block_z", "grid_x", "grid_y", "grid_z", "laneid")


def fixtures(arch):
    image = gpu.compile("launch_geometry", arch=arch)
    out = []
    for shape, (grid, block) in enumerate(SHAPES):
        rows = launch.coordinates(grid, block)
        for field, name in enumerate(FIELDS):
            out.append(gpu.Fixture(f"geometry_{shape}_{name}", image, name, bytes(12 * len(rows)),
                                   [row[field] for row in rows], block=block, grid=grid, exit=True))
    template = gpu.Template(image, "partial")
    stores = [o for o in range(0, len(template.code), 16) if template.decoded(o).klass.mnemonic == "STS"]
    sync = stores[1] + 16
    assert template.decoded(sync).klass.mnemonic == "NOP"
    # ptxas replaces the converged __syncwarp with NOP; retain a real barrier in the fixture.
    image = template.patch({sync: (gpu.encode("warpsync__RIR", arch=arch, **({"Sb": 0xffffffff} if arch == "SM89" else {})),
                                   template.control(sync))})
    for block in PARTIAL_BLOCKS:
        size = prod(block); grid = (2, 2, 1)
        values = [((i + 1) * 0x9e3779b9) & 0xffffffff for i in range(size * prod(grid))]
        data = b"".join(struct.pack("<III", v, 0, 0) for v in values)
        out.append(gpu.Fixture(f"partial_{size}", image, "partial", data, launch.partial(values, size),
                               output_size=8, block=block, grid=grid, scope="block", exit=True,
                               targets=("VOTE", "SHFL", "WARPSYNC", "BAR", "STS", "LDS")))
    return out
