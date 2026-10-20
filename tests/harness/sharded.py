"""Run a function over every streamed shard of the selected languages' cases (`tests.cases.stream`),
keeping only per-item summaries: no process holds a language's cases. Holding them all in pytest,
then forking 32 workers from it, copied a 12.5 GB parent until the OOM killer stopped it (2026-10-20)."""
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from tests.cases import shards


def run(fn, variants, families, exhaustive, workers=None):
    """fn(job) for each job (arch, fp, family, exhaustive, shard), in forkserver workers that build
    their own cases. A pool per architecture: its workers' md and lift caches go with it."""
    for arch in dict.fromkeys(a for a, _ in variants):
        jobs = [(a, f, family, exhaustive, shard) for a, f in variants if a == arch for family in families for shard in shards(a, family)]
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("forkserver")) as pool:
            yield from pool.map(fn, jobs)


def merge(out, item, cases, failures, reports):
    """Accumulate an item's [cases, failures, reports of the first five]."""
    totals = out.setdefault(item, [0, 0, []])
    totals[0] += cases; totals[1] += failures; totals[2] += reports[:5 - len(totals[2])]


def collect(parts):
    """One {item: [cases, failures, reports]} from the workers' summaries."""
    out = {}
    for part in parts:
        for item, totals in part.items(): merge(out, item, *totals)
    return out
