from itertools import product


def coordinates(grid, block):
    return [(tx, ty, tz, bx, by, bz, *block, *grid, (tx + block[0] * (ty + block[1] * tz)) % 32)
            for bz, by, bx in product(range(grid[2]), range(grid[1]), range(grid[0]))
            for tz, ty, tx in product(range(block[2]), range(block[1]), range(block[0]))]


def partial(values, size):
    out = []
    for start in range(0, len(values), size):
        block = values[start:start + size]
        for warp in range(0, size, 32):
            lanes = block[warp:warp + 32]
            ballot = sum((v & 1) << i for i, v in enumerate(lanes))
            high = ((block[-1] ^ block[0]) + (block[-1] ^ lanes[0]) + lanes[0]) & 0xffffffff
            out.extend((high << 32) | ballot for _ in lanes)
    return out
