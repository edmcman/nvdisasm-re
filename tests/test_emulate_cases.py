"""The generated C++ (sass.emulate.cxx) computes the independent references' values on the emulator
cases, as test_lifted_semantics does for the Python evaluator: each shard's instructions compile
into one fragment library. Cases asserting runtime events (`__events`) stay with the evaluator, which
records them. One item per (architecture, fp, family, md class)."""
from functools import cache
from sass.decode import pydecode
from sass.emulate import machine
from tests.harness import lifted, sampling, sharded
from tests.harness.select import arches, families, modes, selected, skipped


def native(job):
    """{item: [cases, failures, reports]} over one shard, in a worker process."""
    arch, fp = job[:2]
    cases = [(item, case) for item, case in lifted.judged(job) if "word" in case.request and "__events" not in case.expected]
    keys = {}
    for _, case in cases:
        word, address = bytes.fromhex(case.request["word"]), case.request.get("address", 0x1000)
        try: compiled = lifted.lift(arch, word, address, fp).status != "opaque"
        except pydecode.NoMatch: compiled = False
        if compiled: keys.setdefault((arch, word, address), len(keys))
    library = machine.Fragments(list(keys), fp) if keys else None

    def execute(arch, semantics, case, runtime):
        request = case.request
        key = (arch, bytes.fromhex(request["word"]), request.get("address", 0x1000))
        observe = [(m["space"], m["address"], m["size"]) for m in request.get("observe_memory", [])]
        return library.execute(keys[key], lifted.state(arch, request), request.get("context") or {}, observe)

    out = {}
    for item, case in cases:
        found = lifted.run(arch, fp, case, execute)[1]
        sharded.merge(out, item, 1, int(bool(found)), [f"  {found}: request {case.request}\n    expected {case.expected}"] if found else [])
    return out


@cache
def results(variants, fams, sample): return sharded.collect(sharded.run(native, variants, fams, sample))


def selection(config):
    return tuple((a, f) for a in arches(config) for f in modes(config)), tuple(families(config)), sampling.configured(config)


def pytest_generate_tests(metafunc):
    if "native_class" not in metafunc.fixturenames: return
    if not selected(metafunc.config): return skipped(metafunc, "native_class", "unmarked", "unmarked tests not selected by -m")
    params = sorted(results(*selection(metafunc.config)))
    metafunc.parametrize("native_class", params, ids=["-".join(p) for p in params])


def test_emulate_cases(native_class, request):
    total, failed, reports = results(*selection(request.config))[native_class]
    assert not failed, f"{failed}/{total} cases failed\n" + "\n".join(reports)
