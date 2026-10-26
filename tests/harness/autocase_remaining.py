"""Memory, collective and environment-contract profiles for the remaining modeled operations.

MUFU SIN/COS profiles use the independently formulated SM89 fixed-point reference.
Synchronization profiles verify environment events, not a simulated multithread scheduler.
"""
from tests.refs import warp, memory, mufu
from tests.harness import autocase_profiles

MEMORY = set('LD LDG LDL LDS ST STG STL STS'.split())
WARP = set('ELECT MATCH REDUX SHFL VOTE VOTEU'.split())
SYNC = set('BAR BMOV BREAK BSSY BSYNC WARPSYNC'.split())
OPS = MEMORY | WARP | SYNC | {'MUFU'}
FAMILIES = {op:'memory' if op in MEMORY else 'threads' if op in WARP else 'control' for op in OPS}
FAMILIES['MUFU'] = 'mufu'


def blocked_reason(op, cls, form):
    if op == 'MUFU' and (not cls.startswith('mufu__') or form.get('mufuop', form.get('op')) not in ('SIN', 'COS')):
        return 'MUFU numerical approximation oracle unavailable; generated error contracts are not value coverage'


def generate(c,k,form,profiles):
    from tests.harness.autocases import Unsupported
    if k.mnemonic == 'MUFU' and not blocked_reason('MUFU', k.name, form):
        if profiles != ['mufu_sincos']: raise Unsupported('incompatible MUFU SIN/COS profile')
        return trigonometric(c, k, form)
    if profiles != ['remaining_forms']: raise Unsupported('incompatible remaining-operation profile')
    if k.mnemonic in MEMORY: return access(c,k,form)
    if k.mnemonic in WARP: return collective(c,k,form)
    return environment(c,k,form)


def trigonometric(c, k, form):
    from tests.harness.autocases import Inputs, Unsupported
    from tests.harness.case import source
    src = source(k, 'Rb', 'Sb', 'URb')
    if src is None or set(form) - {'mufuop', 'op', src+'@absolute', src+'@negate'}:
        raise Unsupported('unhandled MUFU source/modifiers')
    for index, value in enumerate(autocase_profiles.PROFILES['mufu_sincos']['words']):
        s = Inputs(c, k, form)
        if k.operand_types[src].type == 'F64Imm':
            # convertFloatType chooses the encoded F32 bits for SIN/COS.
            s.fields[src] = value
        else:
            s.operand((src,), value, 2)
        result = mufu.sincos(value, form.get('mufuop', form.get('op')),
            form.get(src+'@absolute') == 'set', form.get(src+'@negate') == 'set')
        s.add({s.output(('Rd',), 2 if index % 3 == 0 else 8): result}, form)
    return c


def environment(c,k,form):
    from tests.harness.autocases import Inputs
    spec=autocase_profiles.PROFILES['remaining_forms']
    for index,value in enumerate(spec['words']):
        s=Inputs(c,k,form)
        # Physical register/constant placement is independent of the environment response.
        for n,t in k.operand_types.items():
            if n in form or n=='Pg' or n.endswith(('_bank','_addr','_offset')): continue
            if t.type in ('Register','UniformRegister','SImm','UImm','RSImm','F16Imm','F32Imm','F64Imm','C','CX'):
                if t.type in ('RSImm','F16Imm','F32Imm','F64Imm'): s.fields[n]=0
                else: s.operand((n,), value if t.type not in ('SImm','UImm') else index*4, 2 if n!='Rd' else 8)
        if 'Pp' in k.operand_types: s.predicate(('Pp',),index%2,1)
        active=(index%2) ^ (form.get('Pp@not')=='set') if 'Pp' in k.operand_types else True
        expected={};context={}
        if k.mnemonic=='MUFU': expected={'error':'MUFU approximation'}
        else:
            handler=True
            if 'Rd' in k.operand_types:
                # BMOV's read: the environment's value for the state's encoding.
                handler={str(c.md.enums[k.operand_types['cbu_state'].type][form.get('cbu_state','B0')]):value}
                expected[s.output(('Rd',),8)]=value if active else 0
                s.registers['R8']=0
            context={'synchronization':{k.mnemonic.lower():handler}}
            expected['__events']=int(bool(active))
        s.add(expected,form,context=context,hardware={'skip':'environment synchronization contract or unresolved MUFU approximation'})
    return c


def collective(c,k,form):
    from tests.harness.autocases import Inputs, Unsupported
    for index,mask in enumerate((0xffffffff,0x0000ffff,0xaaaaaaaa)):
        lane=(0,3,5)[index]
        values=[(i%3 if index==0 else (0x80000000+i if i%2 else 0x7fffffff-i) if index==1 else 7) for i in range(32)]
        predicates=[int(i%3==0) if index==0 else int(index==1) for i in range(32)]
        context={'warp':dict(lane=lane,active=mask,values=values,predicates=predicates)}
        s=Inputs(c,k,form);expected={};op=k.mnemonic
        if op in ('VOTE','VOTEU'):
            s.predicate(('Pp',),predicates[lane],1)
            ps=[p ^ (form.get('Pp@not')=='set') for p in predicates]
            ballot,any_,all_=warp.vote_members(ps,[i for i in range(32) if mask>>i&1])
            result={'ANY':any_,'ALL':all_,'EQ':any_==all_}[form['voteop']]
            expected[s.output(('Rd','URd'),8)]=ballot
            expected[s.output(('Pu','UPu'),0)]=int(result)
        elif op=='REDUX':
            s.operand(('Ra',),values[lane],2)
            expected[s.output(('URd','Rd'),8)]=warp.redux('add' if form['op']=='SUM' else form['op'].lower(),[v for i,v in enumerate(values) if mask>>i&1],form.get('sz')=='S32')
        elif op=='MATCH':
            if form.get('sz')=='U64':
                values=[v | ((i%2)<<32) for i,v in enumerate(values)]
                context['warp']['values']=values
            s.operand(('Ra',),values[lane],2,size=8 if form.get('sz')=='U64' else 4)
            selected=[v for i,v in enumerate(values) if mask>>i&1]
            equal=sum(1<<i for i,v in enumerate(values) if mask>>i&1 and v==values[lane])
            if form['op']=='ANY':result=equal
            else:
                _,ok=warp.match_all(selected);result=mask if ok else 0
                expected[s.output(('Pu',),0)]=ok
            expected[s.output(('Rd',),8)]=result
        elif op=='ELECT':
            member=mask if index!=1 else 0xfff0
            if 'URa' in k.operand_types: s.operand(('URa',),member,4)
            else: raise Unsupported('ELECT predicate member selection remains unresolved')
            effective=(~member & 0xffffffff) if form.get('URa@invert')=='set' else member
            leader,flags=warp.elect(mask & effective)
            if leader<0: raise Unsupported('ELECT profile has no active member')
            expected[s.output(('URd',),8)]=leader
            expected[s.output(('Pu',),0)]=flags[lane]
        else:
            b=(5,35,41)[index];control=(31,0,0x181f)[index]
            s.operand(('Ra',),values[lane],2)
            if 'Sb' in k.operand_types: b &= 31
            s.operand(('Rb','Sb'),b,4);s.operand(('Rc','Sc'),control,6)
            result,ok=warp.shfl(form['shflmd'].lower(),values,[b]*32,[control]*32)[lane]
            # Use distinguishable lane ids to identify the chosen source, then apply the active mask.
            target,_=warp.shfl(form['shflmd'].lower(),list(range(32)),[b]*32,[control]*32)[lane]
            if ok and not mask>>target&1: result=0
            expected[s.output(('Rd',),8)]=result
            expected[s.output(('Pu',),0)]=ok
        s.add(expected,form,context=context,hardware={'adapter':'warp'})
    return c


def access(c,k,form):
    from tests.harness.autocases import Inputs,Unsupported
    widths={'U8':1,'S8':1,'U16':2,'S16':2,'32':4,'64':8,'128':16}
    # SM75 LDS's .U sizes are the plain accesses (only LDS: the family leaves SIZE3's .U.128 opaque).
    if k.mnemonic=='LDS':widths|={'U':4,**{'U.'+n:v for n,v in widths.items()}}
    if form.get('sz') not in widths: raise Unsupported('unknown memory access width')
    if form.get('ad','IA')!='IA': raise Unsupported('unresolved memory addressing selector')
    if any(str(v).startswith('INVALID') for v in form.values()):
        raise Unsupported('reserved memory control')
    if 'Rd2' in k.operand_types or 'Rb2' in k.operand_types:
        raise Unsupported('unresolved dual memory registers')
    size=widths[form['sz']];load=k.mnemonic.startswith('LD')
    for index,value in enumerate(autocase_profiles.PROFILES['remaining_forms']['words']):
        if size > 4:
            value = autocase_profiles.PROFILES['remaining_forms']['wide_words'][index] & ((1 << (size * 8)) - 1)
        s=Inputs(c,k,form);base=0x100;offset=(-16,0,16)[index]
        stride={'X1':1,'X4':4,'X8':8,'X16':16}[form.get('stride','X1')]
        ra = 4 if load and size == 16 else 2
        if k.operand_types['Ra'].type=='ZeroRegister':s.fields['Ra']='RZ';base=0;offset=16*(index+1)
        else:
            s.fields['Ra']=ra
            s.registers.update({f'R{ra}':base,f'R{ra+1}':0})
        uniform=0
        for n in ('Ra_URb','Ra_URc'):
            if n in k.operand_types:
                s.fields[n]=4
                uniform=0 if 'memoryDescriptor' in k.operand_types else 0x40
                s.registers.update(UR4=uniform,UR5=0)
        for n in ('Ra_offset','Sa_offset'):
            if n in k.operand_types:s.fields[n]=offset
        address=base*stride+uniform+offset
        space={'LD':'ram','LDG':'ram','ST':'ram','LDL':'localmem','STL':'localmem','LDS':'shared','STS':'shared','STG':'ram'}[k.mnemonic]
        # Generic profiles explicitly choose global memory, with both windows disjoint.
        if k.mnemonic in ('LD','ST'):
            s.registers.update(SR_SWINLO=0x10000,SR_SWINHI=0,SR_SWINSZ=0x100,SR_LWINLO=0x20000,SR_LWINHI=0,SR_LWINSZ=0x100)
            assert memory.generic(address,(0x10000,0x100),(0x20000,0x100))==(space,address)
        data=(value & ((1<<(size*8))-1)).to_bytes(size,'little')
        expected={};extras={}
        if load:
            s.memory.append(dict(space=space,address=address,hex=data.hex()))
            result=memory.constant(data,form['sz'].removeprefix('U.').startswith('S'))
            dest=ra if index==1 else 8
            expected[s.output(('Rd',),dest)]=result&0xffffffff
            for j in range(1,max(1,size//4)):expected['R'+str(dest+j)]=result>>(32*j)&0xffffffff
            if 'Pnz' in k.operand_types:
                pred=index%2;s.predicate(('Pnz',),pred,1)
                expected['P1']=pred
                if not (pred ^ (form.get('Pnz@not')=='set')):
                    for n in list(expected):
                        if n.startswith('R'):expected[n]=0
            if k.mnemonic=='LDG' and 'Pu' in k.operand_types:
                # A distinct old true output, plus Pu/Pnz aliasing on an issued load.
                pu=1 if index==1 and 'Pnz' in k.operand_types else 0
                s.predicate(('Pu',),1,pu)
                expected[s.output(('Pu',),pu)]=memory.load_predicate(1)
                if index==2:
                    s.fields['Pg']=2;s.registers['P2']=0;expected['P2']=0
                    for n in list(expected):
                        if n.startswith('R'):s.registers[n]=expected[n]=0xdeadbeef
                    expected[f'P{pu}']=memory.load_predicate(1,False)
        else:
            s.operand(('Rb',),value,4 if size==16 else 6,size=size)
            if size == 16:
                s.registers.update({f'R{s.fields["Rb"]+j}': value >> (32*j) & 0xffffffff for j in range(4)})
            expected['__memory']=data.hex()
            extras['observe_memory']=[dict(space=space,address=address,size=size)]
        # Exact named runtime effects are legitimate native memory operations.
        effects=['sass_memory_order']+(['sass_memory_descriptor'] if 'memoryDescriptor' in k.operand_types else [])
        expected['__events']=1
        expected['__event_values'] = memory_event(c, k, form)
        if s.fields.get('Pg')==2:
            expected['__events']=0;expected['__event_values']=[]
        # Generic window routing contains branches; value and event checks cover that native form.
        expected['__max_ops']=dict(count=128,userops=effects)
        if k.mnemonic in ('LD','ST') or 'Pnz' in k.operand_types or s.fields.get('Pg')==2:
            expected['__max_ops']['branches']=True
        s.add(expected,form,hardware={'adapter':'memory'},**extras)
    return c


def memory_event(c, k, form):
    """The documented ordering-event ABI, from the test inputs, not lifted semantics."""
    controls = []
    for name in ('sem', 'sco', 'cop', 'cop2', 'private'):
        if name not in k.operand_types: continue
        atom = k.operand_types[name]
        label = form.get(name, atom.default)
        controls.append(c.md.enums[atom.type][label])
    return [dict(op=k.mnemonic, controls=controls)]


def issued_accesses(c):
    """Neutral-variant anchors exercise an issued, nonzero access, from inputs alone.

    A zero-filled load or a zero store into a zeroed arena would not detect a lost
    access. Selection never depends on reference outputs or emulator inspection.
    """
    from sass.decode import pydecode
    from tests.harness.autocases import Unsupported
    out = []
    for case in c:
        decoded = pydecode.decode(c.arch, bytes.fromhex(case.request['word']))
        if 'Pnz' in decoded.env:
            n = decoded.env['Pnz']
            value = 1 if n == 7 else case.request['registers'].get(f'P{n}', 0)
            if not (value ^ decoded.env.get(('Pnz', 'not'), 0)): continue
        if decoded.klass.mnemonic.startswith(('LD', 'ULDC')):
            nonzero = any(any(bytes.fromhex(m['hex'])) for m in case.request.get('memory', []))
        else:
            n = decoded.env.get('Rb', 255)
            nonzero = n != 255 and bool(case.request['registers'].get(f'R{n}', 0))
        if nonzero: out.append(case)
    if not out: raise Unsupported('neutral memory profile needs an issued nonzero access')
    return out
