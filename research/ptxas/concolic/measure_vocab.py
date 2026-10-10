"""Vocabulary reached by accepted PTX in pipeline catalogues, lexed with the pinned ptxas automaton.

python3 measure_vocab.py RUN_DIR... > summary.json
Instruction regions only. Identifiers count only as statement heads naming registered opcodes.
"""
import json, sqlite3, sys
from collections import Counter, defaultdict
from pathlib import Path
from corpus import heads, instruction_region
from lexer_vocab import COMMENTS, HERE, RESERVED, automaton, lex
from pinned import loader

SKIP={543,546,*COMMENTS}  # whitespace, newline, comments

def measure(root,dfa,token_of,registry):
    db=sqlite3.connect(f'file:{root}/catalogue.sqlite?mode=ro',uri=True)
    origin=dict(db.execute('SELECT key,origin FROM observations o WHERE id=(SELECT min(id) FROM observations WHERE key=o.key)'))
    result={}
    for arch, in db.execute('SELECT DISTINCT arch FROM compilations ORDER BY arch'):
        accepted=db.execute('SELECT c.key,s.source FROM compilations c JOIN candidates s USING(key) WHERE c.arch=? AND c.rc=0',(arch,)).fetchall()
        opcodes=set(); reserved=defaultdict(set); origins=Counter()
        for key,source in accepted:
            origins[origin.get(key,'?').split(':')[0]]+=1
            data=Path(source).read_bytes(); span=instruction_region(data)
            if not span: continue
            region=data[span[0]:span[1]]
            tokens=[(region[a:b],action) for a,b,action in lex(dfa,region) if action not in SKIP]
            for text,action in tokens:
                if action in RESERVED: reserved[token_of[action]].add(text.decode())
            opcodes|={h.decode() for h in heads([t for t,_ in tokens])}&registry
        count=lambda table: db.execute(f'SELECT count(*) FROM {table} WHERE arch=?',(arch,)).fetchone()[0]
        result[arch]=dict(accepted=len(accepted),accepted_by_origin=dict(origins),forms=count('forms'),sequences=count('sequences'),
                          opcodes=sorted(opcodes),reserved_total=sum(map(len,reserved.values())),
                          reserved={str(t):sorted(s) for t,s in sorted(reserved.items())})
    return result

if __name__=='__main__':
    vocab=json.loads((HERE/'lexer_vocab.json').read_text())
    registry=json.loads((HERE/'opcode_registry.json').read_text())
    token_of={a['action']:int(t) for t,actions in vocab['classes'].items() for a in actions}
    dfa=automaton(loader('/usr/local/cuda-13.0/bin/ptxas'))
    names=set(registry['names'])|set(registry['roots'])
    json.dump({str(r):measure(Path(r).resolve(),dfa,token_of,names) for r in sys.argv[1:]},sys.stdout,indent=1)
    print()
