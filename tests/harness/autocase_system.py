"""Reference-backed constant loads, special registers, control flow and address queries."""
from tests.harness import autocase_profiles
from tests.harness.case import cbank, pair, source
from tests.refs import control, memory

CONSTANT_OPS = {'LDC','LDCU','ULDC'}
SPECIAL_OPS = {'S2R','CS2R','S2UR'}
FLOW_OPS = {'BRA','CALL','RET','EXIT','NOP'}
OPS = CONSTANT_OPS | SPECIAL_OPS | FLOW_OPS | {'QSPC'}
FAMILIES = {op: 'memory' if op in CONSTANT_OPS | {'QSPC'} else 'control' for op in OPS}


def generate(c,k,form,profiles):
    from tests.harness import autocases
    if profiles != autocase_profiles.names(k.mnemonic,k.name,form):
        raise autocases.Unsupported('incompatible system profiles')
    if k.mnemonic in CONSTANT_OPS: return constants(c,k,form)
    if k.mnemonic in SPECIAL_OPS: return special(c,k,form)
    if k.mnemonic in FLOW_OPS: return flow(c,k,form)
    return query(c,k,form)


def constants(c,k,form):
    from tests.harness import autocases
    if set(form)-{'sz','ad','texunpack'}: raise autocases.Unsupported('unhandled constant-load modifiers')
    if form.get('ad','IA') != 'IA' or form.get('texunpack','notexunpack') != 'notexunpack':
        raise autocases.Unsupported('specialized constant addressing remains manual')
    sz=form['sz']; sizes={'U8':1,'S8':1,'U16':2,'S16':2,'32':4,'64':8}
    if sz not in sizes: raise autocases.Unsupported('unknown constant-load size')
    if 'Sa_bank' not in k.operand_types: raise autocases.Unsupported('indirect constant-bank addressing remains unresolved')
    spec=autocase_profiles.PROFILES['constant_load_forms'];size=sizes[sz]
    for j,word in enumerate(spec['words']):
        s=autocases.Inputs(c,k,form);bank=spec['banks'][j%2];offset=spec['offsets'][j%2]
        base=0;name=source(k,'Ra','URa')
        if name:
            typ=k.operand_types[name].type
            if typ=='ZeroRegister' or name=='URa' and j%3==0:
                s.fields[name]='URZ' if name=='URa' else 'RZ'
            else:
                base=0x100;s.fields[name]=4;s.registers[('UR' if 'Uniform' in typ else 'R')+'4']=base
            s.fields['Ra_offset' if 'Ra_offset' in k.operand_types else 'Sa_offset']=offset if base else abs(offset)
        else:s.fields['Sa_addr']=abs(offset)
        at=base+(offset if base else abs(offset));s.fields['Sa_bank']=bank
        data=(word & ((1 << (8*size))-1)).to_bytes(size,'little')
        s.memory.append(dict(space='cbank',address=(bank<<32)|at,hex=data.hex()))
        result=memory.constant(data,sz.startswith('S'))
        out=s.output(('Rd','URd'),4 if j&1 else 8)
        expected=pair(out,result) if size==8 else {out:result}
        if k.mnemonic in ('LDC','LDCU'):
            expected.update(__max_ops={'count':32,'userops':['sass_memory_order']},__events=1)
        s.add(expected,form,hardware={'adapter':'memory'})
    return c


def special(c,k,form):
    from tests.harness import autocases
    if set(form)-{'sz'} or form.get('sz','32') not in ('32','64'):
        raise autocases.Unsupported('unknown special-register width')
    spec=autocase_profiles.PROFILES['special_register_forms'];enum=c.enum(k,'SRa')
    names={v:n for n,v in enum.items() if n.startswith('SR_')};wide=form.get('sz')=='64'
    for name in spec['sources']:
        if name not in enum:continue
        # CS2R.64 pairs start at even SR codes; SRZ is the explicit zero-pair exception.
        if wide and name!='SRZ' and enum[name]&1:continue
        for word in spec['words']:
            s=autocases.Inputs(c,k,form);s.fields['SRa']=name
            if name=='SRZ':result=0
            else:
                s.registers[name]=word;result=word
                if wide:
                    successor=names.get(enum[name]+1)
                    if successor is None: raise autocases.Unsupported('special-register pair has no named successor')
                    s.registers[successor]=spec['high'];result |= spec['high']<<32
            out=s.output(('Rd','URd'),8)
            s.add(pair(out,result) if wide else {out:result},form,
                  hardware={'skip':'synthetic special-register state; physical read-only register values have separate hardware anchors'})
    return c


def flow(c,k,form):
    from tests.harness import autocases
    op=k.mnemonic
    if set(form)-{'depth','cond','Pp@not','no_atexit','mode','addr','usel','nottid0'}:
        raise autocases.Unsupported('unhandled control-flow modifiers')
    if op=='CALL' and form.get('depth')!='NOINC' or op=='RET' and form.get('depth')!='NODEC':
        raise autocases.Unsupported('call-stack depth transitions remain unresolved')
    spec=autocase_profiles.PROFILES['control_flow_forms'];pc=spec['pc']
    for j,offset in enumerate(spec['offsets'] if op in ('BRA','CALL','RET') else (0,)):
        for pred in spec['predicates']:
            s=autocases.Inputs(c,k,form);s.registers.update(spec['sentinels'])
            if 'Pp' in k.operand_types:s.predicate(('Pp',),pred,0)
            target=None;relative=False
            if op=='BRA': s.fields['sImm']=offset;target=offset;relative=True
            elif op in ('CALL','RET'):
                n=source(k,'Sa')
                if n:
                    typ=k.operand_types[n].type
                    if typ=='RSImm':s.fields[n]=offset;target=offset;relative=True
                    elif typ=='UImm':s.fields[n]=spec['target'];target=spec['target']
                    else:s.operand((n,),spec['target'],4);target=spec['target']
                else:
                    n=source(k,'Ra','URa')
                    if n is None:raise autocases.Unsupported('unsupported control target layout')
                    prefix='UR' if k.operand_types[n].type=='UniformRegister' else 'R'
                    s.fields[n]=4;s.registers.update(pair(prefix+'4',spec['target']))
                    s.fields['Ra_offset' if 'Ra_offset' in k.operand_types else 'Sa_offset']=offset
                    target=spec['target']+offset;relative=form.get('addr')=='REL'
            elif op=='EXIT':target=0
            taken=bool(pred) ^ (form.get('Pp@not')=='set') if 'Pp' in k.operand_types else True
            expected=dict(spec['sentinels'],__counter=control.counter(pc,target,relative,taken))
            if op=='NOP':expected.update(__max_ops={'count':0,'userops':[]},__events=0)
            s.add(expected,form,address=pc,
                  hardware={'skip':'isolated PC and preserved-state assertions; whole-kernel fixtures cover physical control flow'})
    return c


def query(c,k,form):
    from tests.harness import autocases
    if set(form)-{'e','space','input_reg_sz_32_dist','input_reg_sz_64_dist'}:
        raise autocases.Unsupported('unhandled address-query modifiers')
    query_space={'G':'global','L':'local','S':'shared'}.get(form.get('space'))
    if query_space is None:raise autocases.Unsupported('cluster-space query output tag remains unresolved')
    spec=autocase_profiles.PROFILES['address_query_forms'];ranges=spec['ranges']
    for j,space in enumerate(spec['queries']):
        for delta in spec['positions']:
            s=autocases.Inputs(c,k,form);address=ranges[space][0][0]+delta
            offset=spec['offsets'][j%2];n='Ra';typ=k.operand_types[n].type
            uniform='Ra_URb' in k.operand_types
            if typ=='ZeroRegister':s.fields[n]='RZ';base=0;offset=address
            else:base=address-offset-(0x80 if uniform else 0);s.fields[n]=4
            if typ!='ZeroRegister':
                s.registers.update(pair('R4',base) if form.get('e')=='E' else {'R4':base})
            if uniform:s.fields['Ra_URb']=6;s.registers.update(pair('UR6',0x80) if form.get('e')=='E' else {'UR6':0x80})
            if typ=='ZeroRegister' and uniform:offset-=0x80
            s.fields['Ra_offset']=offset
            pu,tag=memory.query(address,ranges,query_space)
            s.fields['Pu']=1;out=s.output(('Rd',),4 if delta==1 and typ!='ZeroRegister' else 8)
            s.add({'P1':pu,out:tag},form,context={'address_spaces':ranges},
                  hardware={'skip':'synthetic address-provenance map; physical 64-bit query cases are retained separately'})
    return c
