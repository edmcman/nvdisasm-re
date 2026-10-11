"""GDB probes for the pinned compiler; output is $PTX_TRACE_NATIVE JSONL.

PTX_TRACE_NATIVE=/tmp/native.jsonl gdb -q -batch -ex 'source .../native.py' \
  --args /usr/local/cuda-13.0/bin/ptxas -arch=sm_75 -o /tmp/add.cubin .../add.ptx
"""
import hashlib
import json
import os
import gdb

PINNED = 'daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2'
with open(gdb.current_progspace().filename, 'rb') as binary:
    if hashlib.sha256(binary.read()).hexdigest() != PINNED:
        raise RuntimeError('Native probe addresses require CUDA 13.0.88 ptxas')
out = open(os.environ['PTX_TRACE_NATIVE'], 'w')
opcodes = ('add', 'sub', 'xor', 'mov', 'st')
lexer_active = False


def reg(name):
    return int(gdb.parse_and_eval('$' + name))


def integer(address, size=8):
    return int.from_bytes(gdb.selected_inferior().read_memory(address, size), 'little')


def string(address):
    return gdb.Value(address).cast(gdb.lookup_type('char').pointer()).string(errors='replace')


def emit(**fields):
    out.write(json.dumps(fields) + '\n')
    out.flush()


class Probe(gdb.Breakpoint):
    def __init__(self, address, kind):
        self.kind = kind
        super().__init__('*' + address, internal=True)

    def stop(self):
        global lexer_active
        if self.kind == 'lexer_start':
            text = string(reg('rbx'))
            lexer_active = any(text.startswith(op + '.') for op in opcodes)
            if lexer_active:
                emit(event=self.kind, text=text[:60], char=reg('rax') & 255,
                     table=hex(reg('rcx')), check=integer(reg('rcx'), 4),
                     next=integer(reg('rcx') + 4, 4))
        elif self.kind == 'lexer_next' and lexer_active:
            emit(event=self.kind, char=reg('rsi') & 255, table=hex(reg('rcx')),
                 check=integer(reg('rcx'), 4), next=integer(reg('rcx') + 4, 4))
        elif self.kind == 'lexer_match_end' and lexer_active:
            emit(event=self.kind, text=string(reg('r13'))[:reg('rbx') - reg('r13')],
                 action=reg('rax'))
            lexer_active = False
        elif self.kind == 'token_copy':
            text = string(reg('rsi'))
            if text in opcodes:
                emit(event=self.kind, text=text, source=hex(reg('rsi')), dest=hex(reg('rdi')))
        elif self.kind == 'token_return':
            text = string(reg('rbx'))
            if text in opcodes:
                emit(event=self.kind, text=text, token_id=reg('rax'))
        elif self.kind in ('hash_result', 'bucket'):
            # Five saved registers sit above the caller return address.
            caller = integer(reg('rsp') + 40)
            if caller not in (0x46c6a8, 0x46c6cf, 0x46c7bc, 0x46c7da):
                return False
            key = string(reg('rbp'))
            if key not in opcodes:
                return False
            table = reg('r13')
            mask = integer(table + 0x28, 4)
            if self.kind == 'hash_result':
                emit(event=self.kind, key=key, hash=reg('rax') & 0xffffffff,
                     mask=mask, caller=hex(caller))
            else:
                index = reg('rax')
                chain = integer(reg('rdx') + index * 8)
                entries = integer(table + 0x58)
                names = []
                if chain:
                    for i in range(1000):
                        n = integer(chain + 4 + i * 4, 4)
                        if n == 0xffffffff:
                            break
                        names.append(string(integer(entries + 16 * n)))
                    else:
                        raise RuntimeError('Unexpected unterminated bucket chain')
                emit(event=self.kind, key=key, mask=mask, index=index,
                     names=names, caller=hex(caller))
        return False


for address, kind in (
    ('0x720f4d', 'lexer_start'), ('0x720f7d', 'lexer_next'),
    ('0x720fde', 'lexer_next'), ('0x720f81', 'lexer_match_end'),
    ('0x723ead', 'token_copy'), ('0x723eb7', 'token_return'),
    ('0x426e9c', 'hash_result'), ('0x426df2', 'bucket'),
):
    Probe(address, kind)
gdb.execute('run')
out.close()
