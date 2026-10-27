"""Complete C++ enumerations. Explicitly opt in with -m native_full."""
import pytest

from sass.emulate import native
from tests.harness import definition_functions, native_full

pytestmark = pytest.mark.native_full


@pytest.mark.parametrize("mode", native.ROUNDING)
@pytest.mark.parametrize("op", ("add", "mul"))
def test_every_half_pair(op, mode):
    spec = native_full.floating(op, mode)
    assert spec.total == 1 << 32
    native_full.complete(native_full.compiled([spec]), 0, spec)


@pytest.mark.parametrize("dst", (32, 64))
def test_every_half_widening(dst):
    spec = native_full.floating("convert", "RN", dst=dst)
    assert spec.total == 1 << 16
    native_full.complete(native_full.compiled([spec]), 0, spec)


@pytest.mark.parametrize("mode", native.ROUNDING)
def test_every_single_to_half(mode):
    spec = native_full.floating("convert", mode, src=32, dst=16)
    assert spec.total == 1 << 32
    native_full.complete(native_full.compiled([spec]), 0, spec)


def test_every_small_definition(real_uses):
    uses = [use for use in real_uses if sum(use[1]) <= 32]
    assert uses
    totals = []
    for start in range(0, len(uses), definition_functions.BATCH):
        batch = uses[start:start + definition_functions.BATCH]
        specs = [native_full.defined(use) for use in batch]
        fn = native_full.compiled(specs, batch)
        for which, spec in enumerate(specs):
            totals.append(native_full.complete(fn, which, spec)[0])
    assert totals == [1 << sum(widths) for _, widths in uses]
