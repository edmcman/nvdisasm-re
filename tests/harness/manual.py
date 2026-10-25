"""Require a concrete reason when keeping hand-written cases for generated operations."""
from functools import wraps


def explained(reason, kind='manual_reasons'):
    """Record `reason` in c.<kind> for every case fn(c) adds: manual_reasons always run,
    sampled_reasons are sampled like any other case."""
    if not reason.strip(): raise ValueError('retained manual cases need a reason')
    def decorate(fn):
        @wraps(fn)
        def run(c):
            start = len(c)
            fn(c)
            reasons = getattr(c, kind, {})
            reasons.update((case.id, reason) for case in c[start:])
            setattr(c, kind, reasons)
        run.manual_reason = reason
        return run
    return decorate


def retained(reason): return explained(reason)


def anchor(k): return k.name.endswith(('__RRR', '__URURUR'))


class Part:
    """One part of an anchored suite: its retained anchors or its sampled remainder."""
    def __init__(self, anchors): self.anchors = anchors

    def sweep(self, k, n):
        """Indices of a sweep over n codes or values on class k: those near 0, n/2 and n of the
        all-register class are anchors."""
        edges = {*range(4), *range(n // 2 - 2, n // 2 + 2), *range(n - 4, n)} if anchor(k) else ()
        return [i for i in range(n) if (i in edges) == self.anchors]

    def whole(self, k):
        """Whether an unswept loop over class k belongs to this part."""
        return anchor(k) == self.anchors


def anchored(reason):
    """Retain `suite(c, part)`'s anchors and leave the rest of its corpus to sampling: every class
    and modifier form keeps its sampled floor, and the complete corpus runs at 100%."""
    def decorate(suite):
        anchors = explained(reason)(lambda c: suite(c, Part(True)))
        rest = explained(reason, 'sampled_reasons')(lambda c: suite(c, Part(False)))
        @wraps(suite)
        def run(c): anchors(c); rest(c)
        run.manual_reason = reason
        return run
    return decorate
