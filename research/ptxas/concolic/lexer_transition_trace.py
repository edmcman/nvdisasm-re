"""GDB validation of the success-only lexer coverage hook; PTX_LEXER_TRACE=output.json."""
import hashlib,json,os
import gdb
PINNED='daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'
with open(gdb.current_progspace().filename,'rb') as f:
    if hashlib.sha256(f.read()).hexdigest()!=PINNED: raise RuntimeError('Wrong ptxas build')
def reg(n): return int(gdb.parse_and_eval('$'+n))
def integer(a,n=4): return int.from_bytes(gdb.selected_inferior().read_memory(a,n),'little')
success=set();failure=set();observed=set()
class Check(gdb.Breakpoint):
    def __init__(self,site,register):
        self.register=register
        super().__init__(site,internal=True)
    def stop(self):
        entry=reg('rcx'); char=reg(self.register)&0xffffffff
        (success if integer(entry)==char else failure).add(entry)
        return False
class Success(gdb.Breakpoint):
    def stop(self):
        entry=reg('rcx');char=(entry-reg('rdx'))//8
        assert integer(entry)==char&0xffffffff, 'Hook reached with failed check'
        observed.add(entry)
        return False
Check('*0x720f4d','rax');Check('*0x720f7d','rsi');Check('*0x720fde','rsi')
Success('*0x720f58',internal=True)
gdb.execute('run')
assert success==observed,'Not all successful checks reached hook'
with open(os.environ['PTX_LEXER_TRACE'],'w') as f:
    json.dump(dict(compiler_sha256=PINNED,successful=sorted(success),failed=sorted(failure),hook=sorted(observed)),f,indent=2)
    f.write('\n')
print('Validated success-only hook:',len(success),'successful entries;',len(failure),'failed entries')
