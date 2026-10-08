"""Rank instruction classes in the real corpus by how often their p-code is opaque or a primitive call.

usage: python3 tests/semantic_census.py SM89 [SM90 ...] [--out DIR]

Static counts: every word of every cubin, weighted by occurrence. A native class
counts as a fallback when a decoded selector violates its recorded selector
guards (other native constraints, such as register-bank guards, are not modelled).
"""
import argparse, collections, json, sys
from multiprocessing import Pool
from pathlib import Path
sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).parent.parent)]
from corpus import cubins
from sass import ir, pydecode
from sass.cubin import elf_arch, text_words
from sass.gen_sleigh import OUT
from sass.operations import PRIMITIVES

def classify(arch, rows, word):
    try: d = pydecode.decode(arch.name, word)
    except Exception: return ('undecodable', '?', '?', '')
    k = d.klass; row = rows.get(k.name)
    if row is None: return ('ungenerated', k.mnemonic, k.name, '')
    status = row['status']
    if status == 'native':
        for name, labels in row.get('selector_guards', {}).items():
            atom = k.operand_types.get(name)
            label = arch.rev_enums.get(atom.type, {}).get(d.env.get(name)) if atom else None
            if label not in labels:
                return ('primitive' if k.mnemonic in PRIMITIVES else 'opaque', k.mnemonic, k.name, f'{name}={label}')
    return (status, k.mnemonic, k.name, '' if status == 'native' else row.get('reason', ''))

def work(args):
    archname, words = args
    arch = ir.load(archname)
    rows = {r['cls']: r for r in json.loads((OUT / f'sass_{archname.lower()}_coverage.json').read_text())}
    return [classify(arch, rows, w) for w in words]

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
