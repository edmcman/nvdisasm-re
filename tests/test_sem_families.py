"""Every semantics family runs on every md class it covers, on every architecture, under the
SLEIGH backend's enumeration: a family that crashes on some class shape fails here, not in a
language build."""
from concurrent.futures import ProcessPoolExecutor
import pytest
from tests.paths import ARCHES


def crashes(arch):
    from sass.gen_sleigh import Gen
    from sass.semantic import Builder
    from sass.sem_family import family_for
    from sass.sem_sleigh import enumerate_rows
    g, out = Gen(arch, "exact"), []
    for k in g.arch.classes:
        fam = family_for(k)
        if not fam or (k.alternate and not k.aliases): continue
        try: enumerate_rows(Builder(g, k, None, g.span_variants(k)[0][0]), fam)
        except Exception as e: out.append(f"{k.name}: {type(e).__name__}: {e}")
    return out


@pytest.fixture(scope="session")
def family_crashes():
    with ProcessPoolExecutor(len(ARCHES)) as pool: return dict(zip(ARCHES, pool.map(crashes, ARCHES)))


@pytest.mark.parametrize("arch", ARCHES)
def test_families_run_on_every_class(arch, family_crashes):
    assert not family_crashes[arch], "\n".join(family_crashes[arch][:10])
