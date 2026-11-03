from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from sass.decode import ir
from sass.md import parse


def test_concurrent_cold_ir_cache(tmp_path, monkeypatch):
    directory = tmp_path / "ir"
    directory.mkdir()
    (directory / "SM89-stale.pickle").write_bytes(b"stale")
    barrier = Barrier(8, timeout=10)
    write = Path.write_bytes

    def load_arch(arch):
        barrier.wait()
        return dict(classes=[], enums={}, tables={}, constants={})

    def write_bytes(path, data):
        size = write(path, data)
        barrier.wait()
        return size

    monkeypatch.setattr(ir, "CACHE", tmp_path)
    monkeypatch.setattr(parse, "load_arch", load_arch)
    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(ir.load.__wrapped__, ["SM89"] * 8))
    assert all(result == results[0] for result in results)
    assert ir.load.__wrapped__("SM89") == results[0]
    assert len(list(directory.iterdir())) == 1
