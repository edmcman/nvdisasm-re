"""Recorded GPU observations (baseline/gpu_observations) against the current shared-case expectations
and comparison, without a GPU: a disagreement means a reference, expectation or comparison change
contradicts what the hardware returned."""
import pytest
from tests.harness import device_evidence, sampling
from tests.harness.select import arches, families


def pytest_generate_tests(metafunc):
    if 'recorded' not in metafunc.fixturenames: return
    params = [(a, f) for a in arches(metafunc.config) if a in ('SM89', 'SM90') for f in families(metafunc.config)]
    metafunc.parametrize('recorded', params, ids=['-'.join(p) for p in params])


def test_gpu_observations_agree(recorded, request):
    arch, family = recorded
    found = device_evidence.summaries(tuple(families(request.config)), sampling.configured(request.config), arch=arch).get(family)
    if not found or not found['verdicts']: pytest.skip(f'{family} has no GPU scenarios on {arch}')
    assert not found['failures'], '\n'.join(f'{i}: {problems}\n  request={r}' for i, r, problems in found['failures'][:8])
