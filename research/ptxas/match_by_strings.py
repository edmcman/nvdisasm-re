"""Map ptxas executable functions to libnvptxcompiler_static.a source files.

Seeds: a string referenced by exactly one library function and exactly one
executable function pairs them. Propagation: for a paired function, its
internal call sequences (externals dropped) are aligned when they have equal
length, pairing callees position by position. Conflicting pairs are dropped.

  python3 match_by_strings.py libfuncs.json exefuncs.json OUT.tsv
(libfuncs.json from lib_functions.py; exefuncs.json from the Ghidra export.)
"""
import collections, json, sys

TEXT_START = 0x403520  # below this: PLT stubs (externals)


def unique_owner(index):
    return {s: next(iter(fs)) for s, fs in index.items() if len(fs) == 1}


def main(libpath, exepath, out):
    lib = json.load(open(libpath))
    exe = {int(a): v for a, v in json.load(open(exepath)).items()}
    key_of = {k.split(':', 1)[1]: k for k in lib}       # hashed symbol -> member:symbol
    lib_calls = {k: [key_of[c] for c in v['calls'] if c in key_of] for k, v in lib.items()}
    exe_calls = {a: [c for c in v['calls'] if c in exe and c >= TEXT_START] for a, v in exe.items()}

    lib_idx, exe_idx = collections.defaultdict(set), collections.defaultdict(set)
    for k, v in lib.items():
        for s in v['strings']: lib_idx[s].add(k)
    for a, v in exe.items():
        for s in v['strings']: exe_idx[s].add(a)
    lu, eu = unique_owner(lib_idx), unique_owner(exe_idx)
    votes = collections.defaultdict(collections.Counter)
    for s in lu.keys() & eu.keys():
        votes[lu[s]][eu[s]] += 1

    pairs, method, bad_lib, bad_exe = {}, {}, set(), set()

    def add(l, e, how):
        if l in bad_lib or e in bad_exe: return False
        if pairs.get(l, e) != e or (e in rev and rev[e] != l):
            bad_lib.add(l); bad_exe.add(e)
            old = pairs.pop(l, None)
            if old is not None: rev.pop(old, None)
            if e in rev: pairs.pop(rev.pop(e), None)
            return False
        if l in pairs: return False
        pairs[l], method[l] = e, how
        rev[e] = l
        return True

    rev = {}
    for l, c in votes.items():
        e, n = c.most_common(1)[0]
        if len(c) == 1: add(l, e, f'strings({n})')
    seeds = len(pairs)
    work = list(pairs)
    while work:
        l = work.pop()
        if l not in pairs: continue
        a, b = lib_calls.get(l, []), exe_calls.get(pairs[l], [])
        if len(a) != len(b):
            a, b = list(dict.fromkeys(a)), list(dict.fromkeys(b))
        if len(a) != len(b) or not a: continue
        for cl, ce in zip(a, b):
            if cl not in pairs and add(cl, ce, 'calls'): work.append(cl)
    with open(out, 'w') as f:
        f.write('address\tsource\tlib_function\tmethod\n')
        for l, e in sorted(pairs.items(), key=lambda kv: kv[1]):
            f.write(f'{e:#x}\t{lib[l]["source"]}\t{l}\t{method[l]}\n')
    print(f'{seeds} string seeds, {len(pairs)} pairs after call propagation, '
          f'{len(bad_lib)} conflicts dropped, {len({lib[l]["source"] for l in pairs})} source files')


if __name__ == '__main__':
    main(*sys.argv[1:])
