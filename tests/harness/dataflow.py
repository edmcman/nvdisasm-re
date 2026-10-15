"""Register dataflow and control flow of generated p-code against md operand roles,
exact spans and BRANCH_TYPE."""
from sass.md import expression
from sass.decode import pydecode
from sass.semantics.family import family_for
from sass.sleigh.gen import ENUM_FILE, REG_FILES, RESOURCE_FILE, Gen
from sass import ghidra_bridge


PREFIX={'B0':1,'B1':2,'B2':3,'B3':4,'H0':2,'H1':4}   # bytes read up to a selected byte or half


def part_selector(g,d,name):
    """The H0/H1/B0-B3 label of the selector modifier following operand `name` in the class's
    FORMAT (before the next comma), or None."""
    fmt=d.klass.format
    at=next((j for j,x in enumerate(fmt) if x.kind=='operand' and x.name==name),None)
    if at is None:return None
    for x in fmt[at+1:]:
        if x.kind=='lit' and x.name==',':return None
        if x.kind=='mod' and x.name in d.klass.operand_types:
            names={n for n in g.arch.enums.get(x.type,{}) if not n.startswith('INVALID')}
            if names and names<={'H0','H1','B0','B1','B2','B3'}:
                v=d.env.get(x.name,g.arch.enums[x.type].get(x.default))
                return g.arch.rev_enums[x.type].get(v)
    return None


def discarded(g, d):
    """Every register the class writes is a predicate set to PT: the native body for discarded
    predicate destinations computes nothing (RZ destinations still compute, into a sink)."""
    outs = [o for o in g.roles.get(d.klass.name, {}).get('operands', []) if 'write' in (o['role'] or [])
            and ENUM_FILE.get(o['type'])]
    return (bool(outs) and all(ENUM_FILE[o['type']] in ('P', 'UP') for o in outs)
            and all(d.env.get(o['name']) == g.register_zero(d.klass, o['name']) for o in outs))


def expected(g, d, optimized=False, ignore=()):
    """md register cells (reads, writes) of decoded instruction d; `ignore` names operands whose reads to skip."""
    reads, writes = set(), set()
    unused=set(ignore)
    if optimized and d.klass.mnemonic in ('LDG','STG','LDL','STL','LD','ST') and d.env.get('e_desc')==0:
        # Implicit pointer forms use their address registers directly.
        unused.update(('Ra_URb','Ra_URc'))
    if optimized and d.klass.mnemonic in ('LOP3','ULOP3') and 'imm8' in d.env:
        lut=d.env['imm8']
        for bit,names in [(2,('Ra','Sa','URa')),(1,('Rb','Sb','URb')),(0,('Rc','Sc','URc'))]:
            # A truth table reads an input only if toggling it changes a result.
            if all((lut>>v&1)==(lut>>(v^(1<<bit))&1) for v in range(8)):
                unused.update(names)
        if d.env.get('Pu',d.env.get('UPu'))==7:unused.update(('Pp','UPp'))
    if optimized and d.klass.mnemonic in ('ISETP','UISETP','FSETP','FSET','HSET2','HSETP2','VISET'):
        name={'FSETP':'fcomp','FSET':'fcomp','HSET2':'cmp','HSETP2':'cmp'}.get(d.klass.mnemonic,'icmp')
        operand=d.klass.operand_types.get(name)
        if (operand and not d.env.get('ex') and
                d.env.get(name) in {g.arch.enums[operand.type].get(n) for n in ('F','T')}):
            unused.update(('Ra','Sa','Rb','Sb','Rc','Sc','URa','URb','URc'))
        ex=d.klass.operand_types.get('ex')
        if (d.klass.mnemonic=='ISETP' and 'Pr' in d.env and ex and
                d.env.get('ex')==g.arch.enums[ex.type].get('noex')):
            unused.add('Pr')
    if optimized and d.klass.name.startswith('imad_pseudo') and 'Rb' in d.klass.operand_types and d.env.get('Rb')==g.register_zero(d.klass,'Rb'):
        unused.update(('Ra','URa'))   # the pseudo-op fixes Rb = RZ: Ra * 0 folds away (IMAD.MOV)
    if optimized and d.klass.mnemonic=='VABSDIFF4':
        red=d.klass.operand_types.get('red');enums=g.arch.enums.get(red.type,{}) if red else {}
        if d.env.get('red')==enums.get('SIMD_MRG'):
            # The generated full-width merge selector always chooses all four
            # difference lanes; its secondary source C is therefore unused.
            unused.update(('Rc','URc','Sc'))
    # An unused compound value also drops reads of its address registers.
    for atom in d.klass.format:
        if atom.name not in unused or atom.type not in ('C','CX','A','DESC'):continue
        for child in d.klass.format[d.klass.format.index(atom)+1:]:
            if child.kind=='lit' and child.name==',':break
            if child.kind=='operand':unused.add(child.name)
    guard = next((a for a in d.klass.format if a.kind == 'guard'), None)
    for o in g.roles.get(d.klass.name, {}).get('operands', []):
        file = ENUM_FILE.get(o['type'])
        if (not file or file != RESOURCE_FILE.get(o['resource']) or not o['role']
                or o['name'] not in d.env or guard and o['name'] == guard.name):
            continue
        span = o['span']
        if isinstance(span, str): span = int(expression.evaluate(span, g.arch, d.env))
        index = d.env[o['name']]
        base, size, count, zero, _ = REG_FILES[file]
        zero = g.register_zero(d.klass, o['name'])
        if index == zero or index + span > count: continue
        cells = set(range(base + size * index, base + size * (index + span)))
        for role in o['role']:
            if role=='read' and o['name'] in unused:continue
            needed=cells
            if (optimized and role=='read' and span==1 and d.klass.mnemonic=='HADD2'
                    and d.klass.name.startswith('hadd2_F32')):
                # HADD2.F32 widens only its low result lane. H1_H0 and H0_H0
                # select the low half directly; H1_H1 selects the high half
                # through a word shift. Modifiers and that shift retain a full
                # word read in generated p-code.
                selector={'Ra':'iswzA','Rc':'iswzB','URc':'iswzB'}.get(o['name'])
                if selector not in d.klass.operand_types and o['name'] in ('Rc','URc'):
                    selector='iswzB_as_C'
                atom=d.klass.operand_types.get(selector) if selector else None
                if atom and not any(d.env.get((o['name'],attr),0)
                                    for attr in ('absolute','negate')):
                    h1h1=g.arch.enums[atom.type].get('H1_H1')
                    if d.env.get(selector)!=h1h1:
                        needed=set(range(base+size*index,base+size*index+2))
            elif optimized and role=='read' and span==1 and (d.klass.mnemonic in ('HADD2','HFMA2','HMUL2','HMNMX2','HSET2','HSETP2')
                                                               or d.klass.mnemonic in ('FHADD2','FHFMA2') and o['name']=='Ra'):
                # A low-half broadcast in HADD2/HFMA2/HMUL2/HMNMX2/HSET2/HSETP2 (and FHADD2/FHFMA2's F16/BF16 A) reads only the two-byte
                # register slice; |x| and -x apply to the half. Two-register CX address inputs
                # retain the entire pointer.
                selectors={'Ra':('iswzA',),'Rb':('iswzB','iswzC_as_B'),
                           'URb':('iswzB','iswzC_as_B'),'Rc':('iswzC','iswzB_as_C'),
                           'URc':('iswzC','iswzB_as_C')}
                if d.klass.mnemonic=='HMNMX2':
                    selectors.update(Rb=('iswzB',),URb=('iswzB',),Sb=('iswzB',))
                if d.klass.mnemonic=='HADD2':selectors.update(Rc=('iswzB','iswzB_as_C'),URc=('iswzB','iswzB_as_C'))
                selector=next((s for s in selectors.get(o['name'],()) if s in d.klass.operand_types),None)
                if selector:
                    atom=d.klass.operand_types[selector]
                    if d.env.get(selector)==g.arch.enums[atom.type].get('H0_H0'):
                        needed=set(range(base+size*index,base+size*index+2))
            if optimized and role=='read' and span==1 and d.klass.mnemonic in ('F2FP','UF2FP','F2IP','F2F','F2I','I2F','FRND'):
                # F2FP/F2IP and the 16/8-bit conversion sources read an operand's half or byte named
                # by the selector after it in FORMAT (extract, extract_B, selB, hsel, bsel): the bytes
                # up to it (SLEIGH truncates to the low bytes, then shifts). The scaled MERGE_C
                # forms, without a selector, read all of C: the scales above the kept low half.
                label=part_selector(g,d,o['name'])
                if label in PREFIX:needed=set(range(base+size*index,base+size*index+PREFIX[label]))
            if optimized and role=='read' and span==1 and d.klass.mnemonic in ('I2IP','UI2IP'):
                # I2IP packs two width-bit lanes above the rest of C: only the bytes that reach the
                # result are read (a lane's low byte without saturation or RELU; C's low 24/28 bits,
                # or its H0/H1 half for bytes, none for halves).
                label=lambda n:g.arch.rev_enums[d.klass.operand_types[n].type].get(d.env.get(n))
                width=int(label('dstfmt')[1:])
                mode=label('satrelu') if 'satrelu' in d.klass.operand_types else 'SAT'
                lo=base+size*index
                if o['name'] in ('Ra','Rb','URb') and not mode.startswith(('SAT','RELU')):
                    needed=set(range(lo,lo+(width+7)//8))
                elif o['name']=='Rc':
                    half=next((label(n) for n in d.klass.operand_types if n.startswith('extract')),None)
                    needed=(set() if width==16 else cells if half=='H1' else   # H1: a word shift
                            set(range(lo,lo+2)) if width==8 else set(range(lo,lo+(32-2*width+7)//8)))
            if (optimized and role=='read' and d.klass.mnemonic in ('SHF','USHF') and o['name'] in ('Rc','URc','Sc')
                    and all(g.arch.rev_enums[d.klass.operand_types[n].type].get(d.env.get(n))==v for n,v in (('dir','L'),('hilo','LO')))):
                needed=set()   # the low word of (C:A) << n is A << n
            if (optimized and role=='read' and span==1 and d.klass.mnemonic in ('P2R','R2P')
                    and o['name'] in ('Rb','URb')):
                needed=set(range(base+size*index,base+size*index+1))   # a 7-bit predicate mask
            if optimized and role=='read' and span==1 and d.klass.mnemonic=='R2P' and o['name']=='Ra':
                at=int(g.arch.rev_enums[d.klass.operand_types['a_bsel'].type][d.env.get('a_bsel',0)][1])
                needed=set(range(base+size*index,base+size*index+at+1))   # the bytes up to the selected one
            if (optimized and role=='read' and span==1 and d.klass.mnemonic=='IDP' and o['name'] in ('Rb','URb')
                    and g.arch.rev_enums[d.klass.operand_types['mode'].type].get(d.env.get('mode','')).lstrip('_')=='2A.LO'):
                needed=set(range(base+size*index,base+size*index+2))   # 2A.LO multiplies B's low two bytes
            if optimized and role=='read' and o['name']=='Rb' and d.klass.mnemonic in ('STG','STS','STL','ST'):
                atom=d.klass.operand_types.get('sz');enums=g.arch.enums.get(atom.type,{}) if atom else {}
                width=next((width for label,width in [('U8',1),('S8',1),('U16',2),('S16',2)]
                            if enums.get(label)==d.env.get('sz')),None)
                if width:needed=set(range(base+size*index,base+size*index+width))
            (writes if role == 'write' else reads).update(needed)
    if (optimized and d.klass.mnemonic=='UP2UR' and d.klass.name=='up2ur__Imm' and
            d.env.get('insert')==g.arch.enums[d.klass.operand_types['insert'].type].get('B0')):
        # The native lowering specializes the immediate mask, so it reads only
        # the selected uniform predicates (higher mask bits are ignored).
        base,size,_,_,_=REG_FILES['UP']
        for bit in range(7):
            if d.env.get('Sb',0)&(1<<bit):reads.add(base+size*bit)
    if optimized and d.klass.mnemonic in ('P2R','R2P'):
        # PR is an implicit packed predicate-register connector in the md. P2R reads P0-P6;
        # R2P writes them all, keeping the masked-off ones (so it reads them too).
        base,size,_,_,_=REG_FILES['P']
        cells=set(range(base,base+size*7))
        reads.update(cells)
        if d.klass.mnemonic=='R2P':writes.update(cells)
    if optimized and family_for(d.klass) and discarded(g, d):
        reads = set()   # every destination is RZ/PT: native code computes nothing
    if guard:
        file = ENUM_FILE[guard.type]
        base, size, _, zero, _ = REG_FILES[file]
        if d.env[guard.name] != zero:
            reads.update(range(base + size * d.env[guard.name], base + size * (d.env[guard.name] + 1)))
    return reads, writes


def expected_flow(g, d):
    """Ghidra flow type implied by the md BRANCH_TYPE (predicated forms may also fall through)."""
    kind = g.props.get(d.klass.name, {}).get('BRANCH_TYPE', 'BRT_NONE')
    return {'jump': kind == 'BRT_BRANCH', 'call': kind == 'BRT_CALL', 'terminal': kind in ('BRT_RETURN', 'BRT_BRANCHOUT')}


def actual(ops):
    reads, writes = set(), set()
    for op in ops:
        for nodes, cells in ((op['inputs'], reads), ([op['output']], writes)):
            for n in nodes:
                if n and n['space'] == 'register' and n['offset'] < 0x810:
                    cells.update(range(n['offset'], n['offset'] + n['size']))
    return reads, writes


def problems(arch, fp, words):
    """Failure descriptions for the given instruction words (bytes)."""
    gen, todo = Gen(arch, fp), []
    for w in words:
        try: todo.append((w, pydecode.decode(arch, w)))
        except pydecode.NoMatch: pass
    out = []
    for (w, d), result in zip(todo, ghidra_bridge.disassemble(arch, fp, [(w, 0) for w, _ in todo], "--dataflow")):
        if "error" in result:
            out.append(f"{d.klass.name}: {result['error']}")
        elif (flow := result["flow"]) != (want_flow := expected_flow(gen, d)):
            out.append(f"{d.klass.name} {w.hex()} {d.text}: flow {flow}, md {want_flow}")
        elif (got := actual(result["ops"])) != (want := expected(gen, d, optimized=not any(
                (op.get("userop") or "").startswith(("sass_opaque_", "sass_prim_")) for op in result["ops"]))):
            out.append(f"{d.klass.name} {w.hex()} {d.text}: reads missing={sorted(want[0] - got[0])} extra={sorted(got[0] - want[0])}; "
                       f"writes missing={sorted(want[1] - got[1])} extra={sorted(got[1] - want[1])}")
    return len(todo), out
