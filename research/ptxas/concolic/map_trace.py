"""Trace string-keyed generic map lookups in pinned ptxas using GDB.
Set PTX_MAP_TRACE to the JSON output path; source in batch GDB, then run.
"""
import collections, hashlib, json, os
import gdb
PINNED = 'daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'
with open(gdb.current_progspace().filename, 'rb') as f:
    if hashlib.sha256(f.read()).hexdigest() != PINNED:
        raise RuntimeError('Requires pinned CUDA 13.0.88 ptxas')
def reg(n): return int(gdb.parse_and_eval('$' + n))
def integer(a, n=8): return int.from_bytes(gdb.selected_inferior().read_memory(a,n), 'little')
def string(a): return gdb.Value(a).cast(gdb.lookup_type('char').pointer()).string(length=256, errors='replace').split('\0')[0]
tables = {}
nonstring = collections.Counter()
class Lookup(gdb.Breakpoint):
    def stop(self):
        table = reg('rdi')
        hashfn, eqfn = integer(table), integer(table+8)
        if (hashfn,eqfn) != (0x427630,0x4277b0):
            nonstring[f'{hashfn:#x}/{eqfn:#x}'] += 1
            return False
        key, caller = string(reg('rsi')), integer(reg('rsp'))
        item = tables.setdefault(hex(table),dict(hash=hex(hashfn), equality=hex(eqfn), queries={}, callers={}, keys=[]))
        item['queries'][key] = item['queries'].get(key,0)+1
        site = hex(caller)
        item['callers'].setdefault(site,{})[key] = item['callers'].get(site,{}).get(key,0)+1
        mask = integer(table+0x28,4)
        entries,buckets = integer(table+0x58),integer(table+0x68)
        names=set(item['keys'])
        for b in range(mask+1):
            chain=integer(buckets+8*b)
            if not chain: continue
            for i in range(10000):
                index=integer(chain+4+4*i,4)
                if index==0xffffffff: break
                names.add(string(integer(entries+16*index)))
            else: raise RuntimeError('Unterminated chain')
        item['keys']=sorted(names)
        item['buckets']=mask+1
        return False
Lookup('*0x426d60',internal=True)
gdb.execute('run')
with open(os.environ['PTX_MAP_TRACE'],'w') as f:
    json.dump(dict(compiler_sha256=PINNED,tables=tables,other_callback_calls=dict(nonstring)),f,indent=2)
    f.write('\n')
