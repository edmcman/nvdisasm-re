"""GDB token/action trace for the pinned ptxas lexer; PTX_PREFIX_TRACE is JSONL output."""
import hashlib,json,os
import gdb
PINNED='daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'
with open(gdb.current_progspace().filename,'rb') as f:
    if hashlib.sha256(f.read()).hexdigest()!=PINNED: raise RuntimeError('Wrong ptxas build')
out=open(os.environ['PTX_PREFIX_TRACE'],'w')
def reg(n): return int(gdb.parse_and_eval('$'+n))
def mem(a,n): return bytes(gdb.selected_inferior().read_memory(a,n))
def integer(a,n=8): return int.from_bytes(mem(a,n),'little')
def emit(**kw): out.write(json.dumps(kw)+'\n');out.flush()
current=None
last_token=None
class Returned(gdb.FinishBreakpoint):
    def __init__(self):
        self.semantic=reg("rdi")
        super().__init__(gdb.newest_frame(),internal=True)
    def stop(self):
        global last_token
        last_token=reg('rax')&0xffffffff
        emit(event='return',token=last_token,caller=hex(reg('rip')),semantic=hex(integer(self.semantic)))
        return False
class Entry(gdb.Breakpoint):
    def stop(self):
        Returned();return False
class Action(gdb.Breakpoint):
    def stop(self):
        action=reg('rax')&0xffffffff
        if action==550: return False
        start=reg('r13'); length=integer(reg('rbp')+0x38,4)
        emit(event='action',text=mem(start,length).decode('utf-8',errors='replace'),action=action,
             target=hex(integer(0x203a5a8+8*action)),state=integer(reg('rbp')+0x4c,4))
        return False
class Reduce(gdb.Breakpoint):
    def stop(self):
        rule=reg('rcx')
        emit(event='reduce',rule=rule,target=hex(integer(0x1d10a68+8*rule)),last_token=last_token,top=hex(integer(reg('rbp'))))
        return False
Reduce('*0x4ce7b0',internal=True)
Entry('*0x720f00',internal=True)
Action('*0x720fb7',internal=True)
gdb.execute('run')
out.close()
