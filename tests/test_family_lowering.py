"""Every semantics family runs on every md class it covers, on the selected architectures, under
the SLEIGH backend's enumeration: a family that crashes on some class shape fails here, not in a
language build."""
import os
from concurrent.futures import ProcessPoolExecutor
import pytest
from tests.harness.select import arches


def crashes(job):
    """(arch, [crash]) over every `shards`-th class of arch, starting at `shard`."""
    from sass.sleigh.gen import Gen
    from sass.semantics.operands import Builder
    from sass.semantics.family import family_for
    from sass.sleigh.lower import enumerate_rows
    arch, shard, shards = job
    g, out = Gen(arch, "exact"), []
    for k in g.arch.classes[shard::shards]:
        fam = family_for(k)
        if not fam or (k.alternate and not k.aliases): continue
        try: enumerate_rows(Builder(g, k, None, g.span_variants(k)[0][0]), fam)
        except Exception as e: out.append(f"{k.name}: {type(e).__name__}: {e}")
    return arch, out


@pytest.fixture(scope="session")
def family_crashes(request):
    selected = arches(request.config)
    shards = max(1, (os.cpu_count() or 1) // len(selected))
    out = {arch: [] for arch in selected}
    with ProcessPoolExecutor() as pool:
        for arch, found in pool.map(crashes, [(a, i, shards) for a in selected for i in range(shards)]): out[arch] += found
    return out


def pytest_generate_tests(metafunc):
    if "lowered_arch" in metafunc.fixturenames: metafunc.parametrize("lowered_arch", arches(metafunc.config))


def test_families_run_on_every_class(lowered_arch, family_crashes):
    assert not family_crashes[lowered_arch], "\n".join(family_crashes[lowered_arch][:10])
