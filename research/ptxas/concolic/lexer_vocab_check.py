"""Check lexer_vocab.lex against the real ptxas lexer's actions on whole PTX files.

python3 lexer_vocab_check.py FILE.ptx...   (runs GDB on the pinned ptxas; needs ptrace access)
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

if 'gdb' in sys.modules:
    import gdb
    out=open(os.environ['PTX_LEXER_ACTIONS'],'w')
    reg=lambda n: int(gdb.parse_and_eval('$'+n))
    mem=lambda a,n: bytes(gdb.selected_inferior().read_memory(a,n))
    class Action(gdb.Breakpoint):
        def stop(self):
            action=reg('rax')&0xffffffff
            if action not in (0,550):  # flex backup and end-of-buffer pseudo-actions
                length=int.from_bytes(mem(reg('rbp')+0x38,4),'little')
                out.write(json.dumps([mem(reg('r13'),length).hex(),action])+'\n')
            return False
    Action('*0x720fb7',internal=True)
    gdb.execute('run'); out.close()
else:
    from lexer_vocab import automaton, lex
    from pinned import loader
    PTXAS='/usr/local/cuda-13.0/bin/ptxas'
    dfa=automaton(loader(PTXAS)); failed=False
    for path in map(Path,sys.argv[1:]):
        with tempfile.NamedTemporaryFile(suffix='.jsonl') as trace:
            subprocess.run(['gdb','-q','-batch','-ex',f'source {Path(__file__).resolve()}','--args',PTXAS,'-arch=sm_75','-o','/dev/null',str(path)],
                           env={**os.environ,'PTX_LEXER_ACTIONS':trace.name},check=True,capture_output=True)
            real=[json.loads(line) for line in Path(trace.name).read_text().splitlines()]
        data=path.read_bytes()
        ours=[[data[a:b].hex(),action] for a,b,action in lex(dfa,data)]
        # ptxas lexes built-in PTX before and after the file; the file is one contiguous run.
        real=[r[:2] for r in real]
        starts=[k for k in range(len(real)-len(ours)+1) if real[k:k+len(ours)]==ours]
        failed|=len(starts)!=1
        print(f'{path}: {len(ours)} actions, {"OK at "+str(starts[0]) if len(starts)==1 else "MISMATCH"}')
        if len(starts)!=1:
            show=lambda t: [bytes.fromhex(t[0]).decode(errors='replace'),t[1]]
            best=max(range(len(real)),key=lambda k: next((i for i,(r,o) in enumerate(zip(real[k:],ours)) if r!=o),len(ours)))
            i=next((i for i,(r,o) in enumerate(zip(real[best:],ours)) if r!=o),None)
            if i is not None: print('  first difference', i, 'real', [show(t) for t in real[best+i:best+i+3]], 'ours', [show(t) for t in ours[i:i+3]])
    sys.exit(failed)
