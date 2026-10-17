"""Require a concrete reason when keeping hand-written cases for generated operations."""
from functools import wraps


def retained(reason):
    if not reason.strip(): raise ValueError('retained manual cases need a reason')
    def decorate(fn):
        @wraps(fn)
        def run(c):
            start = len(c)
            fn(c)
            reasons = getattr(c, 'manual_reasons', {})
            reasons.update((case.id, reason) for case in c[start:])
            c.manual_reasons = reasons
        run.manual_reason = reason
        return run
    return decorate
