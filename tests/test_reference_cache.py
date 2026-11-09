from sass.emulate import reference
from sass.semantics import lifter, ordered


def test_lift_cache_keys_and_request_isolation(monkeypatch):
    calls = []
    def lift(arch, word, pc, fp):
        calls.append((arch, word, pc, fp))
        return lifter.Semantics(arch, "test", "", pc, "native", body=ordered.Block())
    monkeypatch.setattr(lifter, "lift", lift)
    reference.lifted.cache_clear()
    def kernel(arch="SM89", word=bytes(16), pc=0x1000, fp="exact"):
        return reference.Kernel(dict(kernel=dict(code=word.hex(), address=pc, threads=1)), arch, fp)
    try:
        first, second = kernel(), kernel()
        original = first.lift(0x1000)
        original.reason = "request-local mutation"
        assert first.lift(0x1000) is original
        assert second.lift(0x1000) is not original and second.lift(0x1000).reason == ""
        assert len(calls) == 1
        for instance, pc in ((kernel(arch="SM90"), 0x1000), (kernel(word=bytes([1]) * 16), 0x1000),
                             (kernel(pc=0x2000), 0x2000), (kernel(fp="readable"), 0x1000)):
            instance.lift(pc)
        assert calls == [("SM89", bytes(16), 0x1000, "exact"),
                         ("SM90", bytes(16), 0x1000, "exact"),
                         ("SM89", bytes([1]) * 16, 0x1000, "exact"),
                         ("SM89", bytes(16), 0x2000, "exact"),
                         ("SM89", bytes(16), 0x1000, "readable")]
        monkeypatch.setattr(lifter, "lift", lambda *args: lift(*args))
        kernel().lift(0x1000)
        assert len(calls) == 6
    finally: reference.lifted.cache_clear()
