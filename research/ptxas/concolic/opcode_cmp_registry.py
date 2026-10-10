"""GDB: extract the pinned ptxas opcode registry after startup.

PTX_OPCODE_REGISTRY=/tmp/registry.json gdb -q -batch -ex 'source .../opcode_cmp_registry.py' \
  --args /usr/local/cuda-13.0/bin/ptxas -arch=sm_75 -o /dev/null .../generic_sm75.ptx
"""
import hashlib
import json
import os
import gdb

PINNED = 'daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'
with open(gdb.current_progspace().filename, 'rb') as binary:
    if hashlib.sha256(binary.read()).hexdigest() != PINNED:
        raise RuntimeError('Registry addresses require CUDA 13.0.88 ptxas')


def integer(address, size=8):
    return int.from_bytes(gdb.selected_inferior().read_memory(address, size), 'little')


class Registry(gdb.Breakpoint):
    def stop(self):
        context = int(gdb.parse_and_eval('$rdi'))
        table = integer(context + 0x9a8)
        mask = integer(table + 0x28, 4)
        entries, buckets = integer(table + 0x58), integer(table + 0x68)
        names = set()
        for bucket in range(mask + 1):
            chain = integer(buckets + 8 * bucket)
            if not chain:
                continue
            for i in range(1000):
                index = integer(chain + 4 + 4 * i, 4)
                if index == 0xffffffff:
                    break
                address = integer(entries + 16 * index)
                name = gdb.Value(address).cast(gdb.lookup_type('char').pointer()).string()
                names.add(name)
            else:
                raise RuntimeError('Unterminated registry bucket')
        roots = sorted({name.split('.')[0] for name in names})
        with open(os.environ['PTX_OPCODE_REGISTRY'], 'w') as out:
            json.dump(dict(compiler_sha256=PINNED, buckets=mask + 1,
                           names=sorted(names), roots=roots), out, indent=2)
            out.write('\n')
        print(f'Extracted {len(names)} registry names, {len(roots)} roots')
        self.enabled = False
        return False


Registry('*0x46c690', internal=True)
gdb.execute('run')
