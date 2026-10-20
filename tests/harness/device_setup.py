"""Real hardware bindings for logical memory and warp state in shared cases."""
from tests.harness import device_cases


def cx_operand(case):
    d = device_cases.decoded(case.request['word'])
    for i, a in enumerate(d.klass.format):
        if a.type != 'CX': continue
        children = []
        for child in d.klass.format[i + 1:]:
            if child.kind == 'lit' and child.name == ',': break
            if child.kind == 'operand': children.append(child)
        ur = next(c.name for c in children if 'UniformRegister' in c.type)
        offset = next(c.name for c in children if c.type in ('UImm', 'SImm'))
        n = d.env[ur]
        regs = case.request['registers']
        header = regs.get(f'UR{n}', 0) | regs.get(f'UR{n + 1}', 0) << 32
        return n, d.env[offset], (header & ((1 << 45) - 1)) << 4, header >> 45
    raise ValueError(f'{case.id}: no CX operand')


def add_pointer(code, dest, temp, offset=0):
    code.parameter(dest, 72); code.parameter(dest + 1, 76)
    if offset:
        code.mov(f'R{temp}', offset)
        code.emit('imad_wide__RsIR_RIR', Rd=dest, Ra=temp, Sb=1, Rc=dest, fmt='U32')


def cx(code, case, temp, arena):
    ur, offset, base, units = cx_operand(case)
    start = (len(arena) + 255) & ~255
    end = max([offset + 16, *(m['address'] - base + len(bytes.fromhex(m['hex'])) for m in case.request.get('memory', []) if m['space'] == 'ram')])
    if end > 1 << 20 or end < 0: raise ValueError(f'{case.id}: CX backing allocation out of bounds')
    arena.extend(bytes(start + end - len(arena)))
    for m in case.request.get('memory', []):
        if m['space'] == 'cbank': continue
        if m['space'] != 'ram': raise ValueError('CX backing must be global memory')
        at = start + m['address'] - base
        data = bytes.fromhex(m['hex'])
        if at < start or at + len(data) > len(arena): raise ValueError('CX backing outside descriptor base')
        arena[at:at + len(data)] = data
    add_pointer(code, temp, temp + 2, start)
    for i in (0, 1): code.emit('r2ur__noOR', URd=ur + i, Ra=temp + i, Pu='PT')
    code.emit('ushf__URuIUR_URIR', URd=ur, URa=ur, Sb=4, URc=ur + 1, dir='R', fmt='U64', hilo='LO')
    code.emit('ushf__URuIUR_URIR', URd=ur + 1, URa='URZ', Sb=4, URc=ur + 1, dir='R', fmt='U32', hilo='HI')
    code.emit('ulop3_lut__URuIUR_URIR', URd=ur + 1, URa=ur + 1, Sb=units << 13, URc='URZ', imm8=0xfc, UPp='UPT')


ADDRESSED = ('LD', 'ST', 'LDG', 'STG', 'LDL', 'STL', 'LDS', 'STS')


def memory_binding(case):
    """Translate the seeded logical address, retaining its register/offset decomposition."""
    d = device_cases.decoded(case.request['word']); regs = case.request['registers']
    if d.klass.mnemonic in ('LDC', 'ULDC', 'MOV'): return None
    ra = d.env.get('Ra', 255)
    address = regs.get(f'R{ra}', 0) | regs.get(f'R{ra + 1}', 0) << 32
    uniform = next((n for n in ('Ra_URb', 'Ra_URc') if n in d.env and d.klass.mnemonic in ADDRESSED and '_uniform' in d.klass.name), None)
    if uniform:
        u = d.env[uniform]
        address += regs.get(f'UR{u}', 0) | regs.get(f'UR{u + 1}', 0) << 32
    if d.klass.mnemonic in ('LDG', 'STG'): space, logical = 'ram', address
    elif d.klass.mnemonic in ('LDL', 'STL'): space, logical = 'localmem', address
    elif d.klass.mnemonic in ('LDS', 'STS'): space, logical = 'shared', address
    elif d.klass.mnemonic in ('LD', 'ST'):
        for prefix, candidate in [('S', 'shared'), ('L', 'localmem')]:
            base = regs.get(f'SR_{prefix}WINLO', 0) | regs.get(f'SR_{prefix}WINHI', 0) << 32
            if base <= address < base + regs.get(f'SR_{prefix}WINSZ', 0):
                space, logical = candidate, address - base; break
        else: space, logical = 'ram', address
    elif d.klass.mnemonic == 'QSPC': return None
    else: raise ValueError(f'{case.id}: unknown memory opcode {d.klass.mnemonic}')
    return space, logical, ra, uniform


def memory_reason(case):
    d = device_cases.decoded(case.request['word'])
    if 'Ra' in d.env and d.env['Ra'] == 255 and not d.klass.name.startswith(('ld_uniform', 'st_uniform')):
        return 'absolute synthetic address has no relocatable base register'
    binding = memory_binding(case)
    if binding and binding[0] == 'ram' and d.env.get('e') == 0:
        return 'synthetic 32-bit global pointer cannot hold a CUDA allocation; paired E case runs on hardware'
    regions = case.request.get('memory', []) + case.request.get('observe_memory', [])
    if any(m['space'] in ('shared', 'localmem') and m['address'] >= 4096 for m in regions):
        return 'synthetic window boundary or decoy outside realizable shared/local storage'
    if binding and binding[0] in ('shared', 'localmem') and binding[1] >= 4096:
        return 'synthetic address-window boundary outside realizable test storage'
    return ''


def storage_address(code, space, logical, temp, global_base, arena_start):
    if space == 'ram': add_pointer(code, temp, temp + 2, arena_start + logical - global_base)
    elif space == 'shared': code.mov(f'R{temp}', logical)
    elif space == 'localmem':
        code.parameter(temp, -0x138)  # c[0][0x28]: driver's local stack base
        code.mov(f'R{temp + 2}', logical)
        code.emit('iadd3_noimm__RRR_RRR', Rd=temp, Ra=temp, Rb=temp + 2, Rc='RZ')
    else: raise ValueError(f'unknown memory space {space}')


def memory(code, case, temp, arena):
    """Initialize allocations and bind the tested address. Return shared/frame byte counts."""
    d = device_cases.decoded(case.request['word'])
    binding = memory_binding(case)
    regions = case.request.get('memory', []) + [dict(m, hex='00' * m['size']) for m in case.request.get('observe_memory', [])]
    if binding and binding[0] == 'ram':
        offset = d.env.get('Ra_offset', 0)
        bits = int(d.klass.operand_types['Ra_offset'].args.split('/')[0]) if 'Ra_offset' in d.klass.operand_types else 24
        if offset >> bits - 1 & 1: offset -= 1 << bits
        if not any(m['space'] == 'ram' and m['address'] <= binding[1] + offset < m['address'] + len(bytes.fromhex(m['hex'])) for m in regions):
            regions.append(dict(space='ram', address=binding[1] + offset, hex='00' * 16))
    ram = [m for m in regions if m['space'] == 'ram']
    base = min((m['address'] for m in ram), default=0) & ~255
    end = max((m['address'] + len(bytes.fromhex(m['hex'])) for m in ram), default=base)
    if end - base > 1 << 20: raise ValueError(f'{case.id}: sparse global storage needs an explicit binding')
    start = (len(arena) + 255) & ~255
    arena.extend(bytes(start + end - base + 16 - len(arena)))
    code.emit('uldc_const__RCR', URd=4, Sa_bank=0, Sa_addr=0x118, sz='64')
    shared, frame = 0, 0
    for m in regions:
        space, logical = m['space'], m['address']
        if space == 'cbank': continue
        data = bytes.fromhex(m['hex'])
        if space == 'ram':
            arena[start + logical - base:start + logical - base + len(data)] = data; continue
        storage_address(code, space, logical, temp, base, start)
        for i in range(0, len(data), 4):
            part = data[i:i + 4]
            code.mov(f'R{temp + 3}', int.from_bytes(part, 'little'))
            code.emit('sts__sImmOffset' if space == 'shared' else 'stl__sImmOffset', Ra=temp, Ra_offset=i, Rb=temp + 3,
                      sz={1: 'U8', 2: 'U16', 4: '32'}[len(part)])
        if space == 'shared': shared = max(shared, logical + len(data))
        else: frame = max(frame, logical + len(data))
    rebound = ()
    if binding:
        space, logical, ra, uniform = binding
        if space == 'shared': shared = max(shared, logical + 16)
        elif space == 'localmem': frame = max(frame, logical + 16)
        storage_address(code, space, logical, temp, base, start)
        if d.klass.mnemonic in ('LD', 'ST') and space != 'ram':
            # Generic addresses add the runtime window base to a storage offset.
            # Local storage_address already includes the driver's stack offset.
            prefix = 'S' if space == 'shared' else 'L'
            code.emit('s2r_', Rd=temp + 2, SRa=f'SR_{prefix}WINLO')
            code.emit('s2r_', Rd=temp + 3, SRa=f'SR_{prefix}WINHI')
            code.emit('imad_wide__RsIR_RIR', Rd=temp, Ra=temp, Sb=1, Rc=temp + 2, fmt='U32')
        if space == 'shared' and d.klass.mnemonic in ('LDS', 'STS'):
            # Preserve scaled addressing exactly; its original offset is already valid.
            pass
        elif uniform:
            u = d.env[uniform]
            # Retain Ra's contribution; put the remaining pointer in the uniform pair.
            original = case.request['registers'].get(f'R{ra}', 0) if ra != 255 else 0
            code.mov(f'R{temp + 2}', original)
            code.emit('iadd3_noimm__RRR_RRR', Rd=temp, Ra=temp, Rb=temp + 2, Rc='RZ', **{'Rb@negate': 1})
            for i in (0, 1): code.emit('r2ur__noOR', URd=u + i, Ra=temp + i, Pu='PT')
            rebound = (u, u + 1)
        else:
            code.copy(ra, f'R{temp}')
            if space == 'ram' or d.klass.mnemonic in ('LD', 'ST'): code.copy(ra + 1, f'R{temp + 1}')
    # Setup used predicates and UR4/5; restore case state, then the bound address is
    # retained. Explicit descriptor inputs are data under test, not wrapper scratch.
    for i in (4, 5):
        if f'UR{i}' in case.request['registers'] and i not in rebound: code.mov(f'UR{i}', case.request['registers'][f'UR{i}'])
    device_cases.predicates(code, case.request['registers'], temp + 2)
    return (max(shared, 16) + 15) & ~15, (max(frame, 16) + 15) & ~15, base, start


def capture_memory(code, case, temp, output, base, start):
    names = []
    for index, m in enumerate(case.request.get('observe_memory', [])):
        offset = 0
        while offset < m['size']:
            size = next(n for n in (4, 2, 1) if n <= m['size'] - offset and (m['address'] + offset) % n == 0)
            storage_address(code, m['space'], m['address'] + offset, temp, base, start)
            code.emit({'ram': 'ldg__sImmOffset', 'shared': 'lds__sImmOffset', 'localmem': 'ldl__sImmOffset'}[m['space']],
                      Rd=temp + 3, Ra=temp, Ra_offset=0, sz={1:'U8',2:'U16',4:'32'}[size], **({'e':'E'} if m['space']=='ram' else {}))
            code.parameter(temp, 64); code.parameter(temp + 1, 68)
            code.emit('uldc_const__RCR', URd=4, Sa_bank=0, Sa_addr=0x118, sz='64')
            code.emit('stg__sImmOffset', Ra=temp, Ra_offset=output, Rb=temp + 3, sz='32', e='E')
            names.append(f'__memory:{index}:{offset}:{size}')
            output += 4; offset += size
    return output, names


def lane_guard(code, temp, value, predicate=6, mask=False, waiting=False):
    """Lanes outside `value` (a lane id, or a lane mask) exit, or with `waiting` stay alive at a
    convergence barrier: the returned join, emitted after the capture, is where they wait."""
    code.emit('s2r_', Rd=temp + 4, SRa='SR_LANEID')
    if mask:
        code.mov(f'R{temp + 2}', value)
        code.emit('shf__RRR_RRR', Rd=temp + 2, Ra=temp + 2, Rb=temp + 4, Rc='RZ', dir='R', fmt='U32', hilo='LO')
        code.emit('lop3_lut__RuIR_RIR', Rd=temp + 2, Ra=temp + 2, Sb=1, Rc='RZ', imm8=0xc0, Pp='PT')
        code.emit('isetp__RsIR_RIR_noEX', Pu=predicate, Pv='PT', Ra=temp + 2, Sb=0, Pp='PT', icmp='NE', fmt='U32', bop='AND')
    else:
        code.emit('isetp__RsIR_RIR_noEX', Pu=predicate, Pv='PT', Ra=temp + 4, Sb=value, Pp='PT', icmp='EQ', fmt='U32', bop='AND')
    if not waiting: return code.emit('exit_', Pg=predicate, **{'Pg@not': 1})
    start = len(code.words)
    code.emit('nop_'); code.emit('nop_')
    def join():
        to = lambda i: (len(code.words) - i - 1) * 16
        code.words[start] = device_cases.scheduled(device_cases.op('bssy_', barReg='B0', Sa=to(start)))
        code.words[start + 1] = device_cases.scheduled(device_cases.op('bra_', Pg=predicate, **{'Pg@not': 1}, sImm=to(start + 1)))
        code.emit('bsync_', barReg='B0'); code.emit('exit_')
    return join


def warp(code, case, temp, arena):
    context = case.request['context']['warp']; d = device_cases.decoded(case.request['word'])
    # A branch reads no lane values, only which lanes execute it and which of the others still live:
    # every inactive lane exits, or every one waits at a convergence barrier.
    if d.klass.mnemonic == 'BRA':
        waiting = context.get('waiting', 0)
        if waiting not in (0, ~context['active'] & 0xffffffff): raise ValueError(f'{case.id}: waiting lanes must be none or every inactive lane')
        return lane_guard(code, temp, context['active'], mask=True, waiting=bool(waiting))
    values = context.get('predicates') if d.klass.mnemonic in ('VOTE', 'VOTEU') else context.get('values')
    if values is None or len(values) != 32: raise ValueError(f'{case.id}: warp requires 32 lane inputs')
    start = (len(arena) + 255) & ~255
    arena.extend(bytes(start - len(arena)))
    arena.extend(b''.join((v & ((1 << 64) - 1)).to_bytes(8, 'little') for v in values))
    add_pointer(code, temp, temp + 2, start)
    code.emit('s2r_', Rd=temp + 4, SRa='SR_LANEID')
    code.emit('imad_wide__RsIR_RIR', Rd=temp, Ra=temp + 4, Sb=8, Rc=temp, fmt='U32')
    code.emit('uldc_const__RCR', URd=4, Sa_bank=0, Sa_addr=0x118, sz='64')
    if d.klass.mnemonic in ('VOTE', 'VOTEU'):
        code.emit('ldg__sImmOffset', Rd=temp + 2, Ra=temp, Ra_offset=0, sz='32', e='E')
        code.emit('isetp__RsIR_RIR_noEX', Pu=d.env['Pp'], Pv='PT', Ra=temp + 2, Sb=0, Pp='PT', icmp='NE', fmt='U32', bop='AND')
    else:
        code.emit('ldg__sImmOffset', Rd=d.env['Ra'], Ra=temp, Ra_offset=0,
                  sz='64' if any(v > 0xffffffff for v in values) else '32', e='E')
    return lane_guard(code, temp, context['active'], mask=True)


def qspc(code, case, temp, arena):
    d = device_cases.decoded(case.request['word'])
    origin = case.hardware['space']; ra = d.env['Ra']
    if origin == 'global':
        start = (len(arena) + 255) & ~255
        arena.extend(bytes(start + 256 - len(arena)))
        add_pointer(code, temp, temp + 2, start + 32)
    else:
        prefix = 'S' if origin == 'shared' else 'L'
        code.emit('s2r_', Rd=temp, SRa=f'SR_{prefix}WINLO')
        code.emit('s2r_', Rd=temp + 1, SRa=f'SR_{prefix}WINHI')
        if origin == 'local': code.parameter(temp + 2, -0x138)
        else: code.mov(f'R{temp + 2}', 0)
        code.mov(f'R{temp + 3}', 32)
        code.emit('iadd3_noimm__RRR_RRR', Rd=temp + 2, Ra=temp + 2, Rb=temp + 3, Rc='RZ')
        code.emit('imad_wide__RsIR_RIR', Rd=temp, Ra=temp + 2, Sb=1, Rc=temp, fmt='U32')
    code.copy(ra, f'R{temp}'); code.copy(ra + 1, f'R{temp + 1}')
    device_cases.predicates(code, case.request['registers'], temp + 2)
