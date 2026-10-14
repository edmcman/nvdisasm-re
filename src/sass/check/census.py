"""Rank instruction classes in the real corpus by how often their p-code is opaque or a primitive call.

usage: python -m sass.check.census SM89 [SM90 ...] [--out DIR]

Static counts: every word of every cubin, weighted by occurrence. Each word's status is what its
family decides for that encoding (`sass.semantics.lift.lift`, the same family the SLEIGH selector
guards come from): native, primitive (a `primitive()` row, or an unmodelled encoding of a family
that falls back to its primitive) or opaque. Classes without a family keep their manifest status.
"""
import argparse, collections, json
from multiprocessing import Pool
from pathlib import Path
from sass.oracle.corpus import cubins
from sass.decode import pydecode
from sass.semantics import lift as sem_lift
from sass.decode.cubin import elf_arch, text_words
from sass.sleigh.gen import OUT
from sass.semantics.operations import PRIMITIVES

def classify(archname, rows, word):
    try: lifted = sem_lift.lift(archname, word)
    except Exception: return ('undecodable', '?', '?', '')
    row = rows.get(lifted.cls)
    if row is None: return ('ungenerated', '?', lifted.cls, '')
    op = row['opcode']
    if lifted.reason == 'no family': return (row['status'], op, lifted.cls, '' if row['status'] == 'native' else row.get('reason', ''))
    status = 'primitive' if lifted.status == 'opaque' and op in PRIMITIVES else lifted.status
    return (status, op, lifted.cls, '' if status == 'native' else lifted.reason)

def work(args):
    archname, words = args
    rows = {r['cls']: r for r in json.loads((OUT / f'sass_{archname.lower()}_coverage.json').read_text())}
    return [classify(archname, rows, w) for w in words]

def census(archname, pool):
    counts = collections.Counter()
    for p in cubins():
        data = p.read_bytes()
        if elf_arch(data) == archname: counts.update(text_words(data))
    words = list(counts)
    chunks = [(archname, words[i:i + 20000]) for i in range(0, len(words), 20000)]
    result = collections.Counter()
    for chunk, keys in zip(chunks, pool.imap(work, chunks)):
        for w, key in zip(chunk[1], keys): result[key] += counts[w]
    return result

def report(archname, result):
    total = sum(result.values())
    by_status = collections.Counter()
    for (status, *_), n in result.items(): by_status[status] += n
    lines = [f'## {archname}: {total:,} instruction words',
             ', '.join(f'{s} {n / total:.1%}' for s, n in by_status.most_common())]
    for status in ('opaque', 'primitive'):
        by_op = collections.Counter(); why = collections.defaultdict(collections.Counter)
        for (s, op, cls, reason), n in result.items():
            if s == status: by_op[op] += n; why[op][f'{cls} {reason}'.strip()] += n
        lines.append(f'### {status}: top mnemonics')
        for op, n in by_op.most_common(25):
            top = '; '.join(f'{r} ({m / n:.0%})' for r, m in why[op].most_common(3))
            lines.append(f'{op:12} {n / total:6.2%}  {top}')
    return '\n'.join(lines)

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('archs', nargs='+'); ap.add_argument('--out', type=Path)
    a = ap.parse_args()
    with Pool() as pool:
        for archname in a.archs:
            result = census(archname, pool)
            print(report(archname, result), flush=True)
            if a.out:
                a.out.mkdir(parents=True, exist_ok=True)
                (a.out / f'{archname}.json').write_text(json.dumps([[*k, n] for k, n in result.most_common()]))

if __name__ == '__main__':
    main()
