"""Class-specific native overrides and explicit instruction primitives.

Unsupported selector combinations branch to complete-input opaque operations.
This module does not equate PTX syntax with undocumented SASS variants.
"""
from dataclasses import dataclass

TIER_B = set('IADD3 IMAD LOP3 SHF LEA IABS IMNMX ISETP SEL PRMT MOV S2R CS2R LDC FADD FMUL FFMA FSETP F2I I2F MUFU LDG STG LDS STS LDL STL BRA EXIT CALL RET BSSY BSYNC BAR SHFL VOTE'.split())
PRIMITIVES = set('FADD FMUL FFMA FSETP F2I I2F MUFU PRMT SHFL VOTE BAR BSSY BSYNC'.split())

@dataclass(frozen=True)
class Override:
    family: str
    implementation: str

REGISTRY = {op: Override(op,'primitive' if op in PRIMITIVES else 'native') for op in TIER_B}


def emit(b):
    op=b.k.mnemonic
    if op not in REGISTRY:return 'opaque','outside original Tier B list'
    if op in PRIMITIVES:
        guards=b.bank_guards()
        if guards:
            b.body += [f'if ({condition}) goto <bankopaque>;' for condition in guards]+b.call('prim')+['goto <bankdone>;','<bankopaque>']+b.call()+['<bankdone>']
        else:b.body += b.call('prim')
        return 'primitive','runtime validates selectors; unsupported combinations raise'
    fn=globals().get('emit_'+op)
    if fn is None:return 'opaque','class variant not yet described by a native override'
    # Validate shape before writing any native code.
    if not fn(b,check=True):return 'opaque',getattr(b,'unsupported_reason','unsupported class shape')
    start=len(b.body)
    b.body += ['if (0:1 != 0) goto <opaque>;']
    b.body += [f'if ({condition}) goto <opaque>;' for condition in b.bank_guards()]
    for name,value in b.values.items():
        if '@' in name and name.split('@')[1] not in ('absolute','negate','invert','not'):
            b.body.append(f'if ({value.symbol} != 0) goto <opaque>;')
    fn(b,check=False)
    if start==0:
        # Keep the reusable native calculation separate from the class-specific
        # fallback ABI. No local from a compound operand crosses this boundary.
        import re
        from sass.gen_sleigh import pattern
        code=' '.join(['SEM_VALID = 1;']+b.body[start:]+['goto <done>;','<opaque>','SEM_VALID = 0;','<done>'])
        names=set(re.findall(r'\b\w+\b',code))
        refs=[r for r in b.refs if r[1] in names]
        table=b.g.subtable('native', [('""',pattern([],refs),'',code)])
        b.g.temporaries['SEM_VALID']=1
        del b.body[start:]
        b.refs.append(('sub',table))
        b.body += [f'build {table};','if (SEM_VALID != 0) goto <done>;']+b.call()+['<done>']
    else:
        b.body += ['goto <done>;','<opaque>']+b.call()+['<done>']
    return 'native','selector guards retain complete opaque fallbacks'


def require(b,name,labels):
    if name not in b.values:return
    atom=b.k.operand_types.get(name)
    if not atom or atom.type not in b.g.arch.enums:return
    b.selector_guards[name]=list(labels)
    vals={b.g.arch.enums[atom.type][x] for x in labels if x in b.g.arch.enums[atom.type]}
    if not vals:b.body.append('goto <opaque>;');return
    v=b.values[name].symbol
    b.body.append('if ('+' && '.join(f'{v} != {x}' for x in sorted(vals))+') goto <opaque>;')


def operands(b,*groups):
    return [next((n for n in group.split('/') if n in b.values),None) for group in groups]


def emit_MOV(b,check=False):
    src=operands(b,'Sb/Sa/Ra/URa')[0]
    if check:return src is not None and 'Rd' in b.outputs
    if 'PixMaskU04' in b.values:b.body.append(f'if ({b.values["PixMaskU04"].symbol} != 15) goto <opaque>;')
    dst=b.outputs['Rd'];b.body.append(f'{dst.symbol} = {b.cast(src,dst.size)};')


def emit_IABS(b,check=False):
    src=operands(b,'Sb/Rb/Ra')[0]
    if check:return src is not None and 'Rd' in b.outputs and b.outputs['Rd'].size==4
    v=b.flag(src);t=b.local(4,v)
    b.body += [f'if ({t.symbol} s>= 0) goto <nonnegative>;',f'{t.symbol} = -{t.symbol};','<nonnegative>',f'{b.outputs["Rd"].symbol} = {t.symbol};']


def emit_SEL(b,check=False):
    a,c,p=operands(b,'Ra/Sa','Rb/Sb','Pp')
    if check:return all((a,c,p)) and 'Rd' in b.outputs
    av,cv,pv=b.flag(a),b.flag(c),b.flag(p)
    t=b.local(b.outputs['Rd'].size,av)
    b.body += [f'if ({pv} != 0) goto <selected>;',f'{t.symbol} = {cv};','<selected>',f'{b.outputs["Rd"].symbol} = {t.symbol};']


def emit_IMAD(b,check=False):
    a,c,d=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:
        if not (all((a,c,d)) and 'Rd' in b.outputs and all(b.values[n].size<=8 for n in (a,c,d))):return False
        mode=b.k.operand_types.get('wide')
        labels=b.g.arch.enums.get(mode.type,{}) if mode else {}
        if set(labels)&{'HI','WIDE'} and b.k.operand_types[d].type in ('C','CX') and b.values[d].size<8:
            b.unsupported_reason='32-bit constant addend extension for HI/WIDE is unverified'
            return False
        return True
    if 'x' in b.values:require(b,'x',['nox','NOX']) # unresolved carry modes stay opaque
    if any(n in b.values for n in ('Pp','Pq')):b.body.append('goto <opaque>;')
    require(b,'fmt',['U32','S32'])
    for name in b.outputs:
        if name!='Rd':
            idx=b.scalar(name,b.k.operand_types[name]);b.body.append(f'if ({idx.symbol} != 7) goto <opaque>;')
    aa,bb=b.flag(a),b.flag(c)
    x=b.local(8,f'zext({aa})'); y=b.local(8,f'zext({bb})')
    fmt=b.values.get('fmt')
    if fmt:
        signed=b.g.arch.enums[b.k.operand_types['fmt'].type].get('S32',1)
        b.body += [f'if ({fmt.symbol} != {signed}) goto <unsignedmul>;',f'{x.symbol} = sext({aa});',f'{y.symbol} = sext({bb});','<unsignedmul>']
    product=b.local(8,f'{x.symbol} * {y.symbol}')
    wide=b.k.operand_types.get('wide'); labels=b.g.arch.enums.get(wide.type,{}) if wide else {}
    rc=b.flag(d)
    if b.values[d].size<8:rc=b.local(8,f'zext({rc})').symbol
    t=b.local(8,f'{product.symbol} + {rc}')
    if 'HI' in labels:
        mode=b.values['wide'].symbol
        b.body += [f'if ({mode} != {labels["HI"]}) goto <mullo>;',f'{t.symbol} = {t.symbol} >> 32;','<mullo>']
    for name,out in b.outputs.items():
        if name=='Rd':b.body.append(f'{out.symbol} = {t.symbol}{":4" if out.size<8 else ""};')
        else:b.body.append(f'{out.symbol} = 0;')


def emit_IADD3(b,check=False):
    a,c,d=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:return all((a,c,d)) and 'Rd' in b.outputs and all(b.values[n].size==4 for n in (a,c,d))
    # .X's carry-input convention is deliberately not guessed.
    if any(n in b.values for n in ('Pp','Pq')):b.body.append('goto <opaque>;')
    aa,bb,cc=(b.flag(n) for n in (a,c,d))
    first=b.local(8,f'zext({aa}) + zext({bb})')
    low=b.local(4,f'{first.symbol}:4');total=b.local(8,f'zext({low.symbol}) + zext({cc})')
    # Predicate/carry conventions require independent SASS evidence. Only PT
    # destinations are handled here; other cases go through the complete fallback.
    for name,out in b.outputs.items():
        if name!='Rd':
            idx=b.scalar(name,b.k.operand_types[name]);b.body.append(f'if ({idx.symbol} != 7) goto <opaque>;')
    b.body.append(f'{b.outputs["Rd"].symbol} = {total.symbol}:4;')
    for name,out in b.outputs.items():
        if name!='Rd':b.body.append(f'{out.symbol} = 0;')


def emit_LOP3(b,check=False):
    a,c,d,p=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc','Pp');lut=operands(b,'imm8')[0]
    if check:return all((a,c,d,lut)) and 'Rd' in b.outputs
    require(b,'pop',['POR','PAND'])
    av,bv,cv=(b.flag(n) for n in (a,c,d));lv=b.values[lut].symbol
    result=b.local(4,'0')
    for i in range(8):
        term=' & '.join((v if i&(1<<(2-j)) else f'~{v}') for j,v in enumerate((av,bv,cv)))
        bit=b.local(4,f'({lv} >> {i}) & 1')
        b.body.append(f'{result.symbol} = {result.symbol} | (({term}) & -{bit.symbol});')
    pred=b.local(1,f'{result.symbol} != 0')
    if p:
        pv=b.flag(p);pop=b.values.get('pop')
        if pop:
            pand=b.g.arch.enums[b.k.operand_types['pop'].type]['PAND']
            b.body += [f'if ({pop.symbol} == {pand}) goto <pand>;',f'{pred.symbol} = {pred.symbol} | {pv};','goto <pdone>;','<pand>',f'{pred.symbol} = {pred.symbol} & {pv};','<pdone>']
    for name,out in b.outputs.items():b.body.append(f'{out.symbol} = {result.symbol if name=="Rd" else pred.symbol};')


def emit_SHF(b,check=False):
    a,c,d=operands(b,'Ra/Sa','Rb/Sb','Rc/Sc')
    if check:return all((a,c,d)) and 'Rd' in b.outputs
    require(b,'fmt',['U32','S32','U64','S64']);require(b,'dir',['L','R']);require(b,'cw',['C','W']);require(b,'hilo',['LO','HI'])
    av,shift,cv=(b.flag(n) for n in (a,c,d));n=b.local(4,shift)
    limit=b.local(4,'32'); fmt=b.values['fmt'].symbol
    enums=b.g.arch.enums[b.k.operand_types['fmt'].type]
    b.body += [f'if ({fmt} == {enums["U32"]} || {fmt} == {enums["S32"]}) goto <shiftwidth>;',f'{limit.symbol} = 64;','<shiftwidth>']
    wrap=b.g.arch.enums[b.k.operand_types['cw'].type]['W'];cw=b.values['cw'].symbol
    b.body += [f'if ({cw} == {wrap}) goto <wrap>;',f'if ({n.symbol} <= {limit.symbol}) goto <countdone>;',f'{n.symbol} = {limit.symbol};','goto <countdone>;','<wrap>',f'{n.symbol} = {n.symbol} & ({limit.symbol} - 1);','<countdone>']
    t=b.local(8,f'(zext({cv}) << 32) | zext({av})')
    right=b.g.arch.enums[b.k.operand_types['dir'].type]['R']
    b.body += [f'if ({b.values["dir"].symbol} == {right}) goto <right>;',f'{t.symbol} = {t.symbol} << {n.symbol};','goto <shifted>;','<right>']
    b.body += [f'if ({fmt} == {enums["S32"]} || {fmt} == {enums["S64"]}) goto <signedshift>;',f'{t.symbol} = {t.symbol} >> {n.symbol};','goto <shifted>;','<signedshift>',f'{t.symbol} = {t.symbol} s>> {n.symbol};','<shifted>']
    hi=b.g.arch.enums[b.k.operand_types['hilo'].type]['HI']
    b.body += [f'if ({b.values["hilo"].symbol} != {hi}) goto <extract>;',f'{t.symbol} = {t.symbol} >> 32;','<extract>',f'{b.outputs["Rd"].symbol} = {t.symbol}:4;']


def comparison(b,a,c,name='icmp'):
    enums=b.g.arch.enums[b.k.operand_types[name].type];sel=b.values[name].symbol
    require(b,name,['F','LT','EQ','LE','GT','NE','GE','T'])
    res=b.local(1,'0');fmt=b.values.get('fmt');signed=b.g.arch.enums.get('FMT',{}).get('S32',1)
    for label,oper in [('F',None),('LT','<'),('EQ','=='),('LE','<='),('GT','>'),('NE','!='),('GE','>='),('T',None)]:
        if label not in enums:continue
        skip='cmp'+str(b.serial);b.serial+=1
        b.body.append(f'if ({sel} != {enums[label]}) goto <{skip}>;')
        if oper is None:b.body.append(f'{res.symbol} = {1 if label=="T" else 0};')
        elif oper in ('==','!=') or not fmt:b.body.append(f'{res.symbol} = {a} {oper} {c};')
        else:
            s='signed'+str(b.serial);d='cmpdone'+str(b.serial);b.serial+=1
            b.body += [f'if ({fmt.symbol} == {signed}) goto <{s}>;',f'{res.symbol} = {a} {oper} {c};',f'goto <{d}>;',f'<{s}>',f'{res.symbol} = {a} s{oper} {c};',f'<{d}>']
        b.body.append(f'<{skip}>')
    return res.symbol


def combine(b,cond,p):
    require(b,'bop',['AND','OR','XOR']);v=b.values['bop'].symbol;en=b.g.arch.enums[b.k.operand_types['bop'].type]
    out=b.local(1,f'{cond} & {p}')
    b.body += [f'if ({v} != {en["OR"]}) goto <notor>;',f'{out.symbol} = {cond} | {p};','<notor>',f'if ({v} != {en["XOR"]}) goto <notxor>;',f'{out.symbol} = {cond} ^ {p};','<notxor>']
    return out.symbol


def emit_ISETP(b,check=False):
    a,c,p=operands(b,'Ra/Sa','Rb/Sb','Pp')
    if check:return all((a,c,p)) and 'icmp' in b.values and 'bop' in b.values and b.values[a].size==b.values[c].size
    if 'ex' in b.values or 'Pr' in b.values:b.body.append('goto <opaque>;')
    require(b,'fmt',['U32','S32'])
    cond=comparison(b,b.flag(a),b.flag(c)); pv=b.flag(p)
    yes=combine(b,cond,pv)
    # Use unique labels for the complementary predicate's boolean combination.
    old=b.body[:]; inv=b.local(1,f'!{cond}')
    before=len(b.body);no=combine(b,inv.symbol,pv)
    b.body[before:]=[s.replace('notor','notor2').replace('notxor','notxor2') for s in b.body[before:]]
    for name,out in b.outputs.items():b.body.append(f'{out.symbol} = {yes if name=="Pu" else no};')


def emit_IMNMX(b,check=False):
    a,c,p=operands(b,'Ra/Sa','Rb/Sb','Pp')
    if check:return all((a,c,p)) and 'Rd' in b.outputs and all(b.values[n].size==4 for n in (a,c)) and b.outputs['Rd'].size==4 and set(b.outputs)=={'Rd'}
    require(b,'fmt',['U32','S32']); av,cv,pv=b.flag(a),b.flag(c),b.flag(p)
    less=b.local(1,f'{av} < {cv}');fmt=b.values.get('fmt')
    if fmt:b.body += [f'if ({fmt.symbol} == 0) goto <minunsigned>;',f'{less.symbol} = {av} s< {cv};','<minunsigned>']
    res=b.local(4,cv)
    b.body += [f'if ({less.symbol} != {pv}) goto <minselected>;',f'{res.symbol} = {av};','<minselected>',f'{b.outputs["Rd"].symbol} = {res.symbol};']


def emit_LEA(b,check=False):
    a,c=operands(b,'Ra','Rb/Sb');scale=operands(b,'scaleU5')[0]
    if check:return all((a,c,scale)) and 'Rd' in b.outputs and 'LO' in b.g.arch.enums.get(b.k.operand_types['hilo'].type,{})
    require(b,'hilo',['LO']);require(b,'sx32',['nosx32','NOSX32'])
    if any(n in b.values for n in ('Pp','Pr','Rc','Sc')):b.body.append('goto <opaque>;')
    for name in b.outputs:
        if name!='Rd':idx=b.scalar(name,b.k.operand_types[name]);b.body.append(f'if ({idx.symbol} != 7) goto <opaque>;')
    t=b.local(8,f'(zext({b.flag(a)}) << {b.values[scale].symbol}) + zext({b.flag(c)})')
    for name,out in b.outputs.items():b.body.append(f'{out.symbol} = {t.symbol+":4" if name=="Rd" else "0"};')


def emit_S2R(b,check=False):
    if check:return 'SRa' in b.values and 'Rd' in b.outputs
    fn='sass_read_special';b.g.pcodeops.add(fn);out=b.outputs['Rd']
    b.body.append(f'{out.symbol} = {fn}({int(b.g.arch.name[2:])}:4, {b.values["SRa"].symbol});')

emit_CS2R=emit_S2R


def emit_BRA(b,check=False):
    if check:return 'sImm' in b.values and 'Pp' in b.values
    require(b,'depth',['nodepth']);require(b,'cond',['nocond','U'])
    if any(n in b.values for n in ('URb','UPq')):b.body.append('goto <opaque>;')
    atom=b.k.operand_types['sImm'];target=b.g.branch_target(b.k,atom);b.refs.append(('sub',target))
    b.body.append(f'if ({b.flag("Pp")} != 0) goto {target};')


def emit_EXIT(b,check=False):
    if check:return True
    require(b,'mode',['noexit_mode'])
    if any(n in b.values for n in ('Ra','URa')):b.body.append('goto <opaque>;')
    if 'Pp' in b.values:b.body.append(f'if ({b.flag("Pp")} == 0) goto <exitskip>;')
    b.body.append('return [0:8];')
    if 'Pp' in b.values:b.body.append('<exitskip>')


def emit_CALL(b,check=False):
    if check:return 'Pp' in b.values and any(n in b.values for n in ('sImm','Sa','Ra','URa'))
    require(b,'depth',['NOINC','noinc','nodepth'])
    b.body.append(f'if ({b.flag("Pp")} == 0) goto <callskip>;')
    if 'sImm' in b.values:
        atom=b.k.operand_types['sImm'];target=b.g.branch_target(b.k,atom);b.refs.append(('sub',target));b.body.append(f'call {target};')
    else:
        name=operands(b,'Sa/Ra/URa')[0];target=b.cast(name,8)
        b.body.append(f'call [{target}];')
    b.body.append('<callskip>')


def emit_RET(b,check=False):
    if check:return any(n in b.values for n in ('Ra','URa')) and 'Pp' in b.values
    require(b,'addr',['ABS']);require(b,'depth',['NODEC'])
    name=operands(b,'Ra/URa')[0];off=operands(b,'Ra_offset/Sa_offset')[0]
    target=b.local(8,f'{b.cast(name,8)} + {b.cast(off,8,True) if off else "0"}')
    b.body.append(f'if ({b.flag("Pp")} == 0) goto <retskip>;');b.body += [f'return [{target.symbol}];','<retskip>']


def memory(b,op,check):
    load=op.startswith('LD');space='cbank' if op=='LDC' else 'shared' if op in ('LDS','STS') else 'localmem' if op in ('LDL','STL') else 'ram'
    if check:return ('sz' in b.values and 'Ra' in b.values and ('Rd' in b.outputs if load else 'Rb' in b.values)
                     and bool(set(b.g.arch.enums[b.k.operand_types['sz'].type]) & {'U8','S8','U16','S16','32','64','128'}))
    if any(n in b.values for n in ('memoryDescriptor','Rd2','Rb2','Ra_URb','Ra_URc')):b.body.append('goto <opaque>;')
    if 'stride' in b.values:require(b,'stride',['X1','1','nostride'])
    if 'ad' in b.values:require(b,'ad',['IA'])
    if 'sp2' in b.values:require(b,'sp2',['nosp2'])
    if 'Pnz' in b.values:
        idx=b.scalar('Pnz',b.k.operand_types['Pnz']);b.body.append(f'if ({idx.symbol} != 7) goto <opaque>;')
    # Predicate-result variants are not assumed to mean successful/nonzero loads.
    for name in b.outputs:
        if name!='Rd':idx=b.scalar(name,b.k.operand_types[name]);b.body.append(f'if ({idx.symbol} != 7) goto <opaque>;')
    base=b.cast('Ra',8);off=operands(b,'Ra_offset/Sa_offset')[0]
    addr=b.local(8,f'{base} + {b.cast(off,8,True) if off else "0"}')
    if space=='cbank':
        bank=operands(b,'Sa_bank')[0]
        if not bank:b.body.append('goto <opaque>;');return
        low=b.local(4,f'{addr.symbol}:4');b.body.append(f'{addr.symbol} = ({b.cast(bank,8)} << 32) | zext({low.symbol});')
    fn='sass_memory_order';b.g.pcodeops.add(fn)
    controls=[b.values[n].symbol for n in ('sem','sco','cop','cop2','private') if n in b.values]
    b.body.append(f'{fn}({int(b.g.arch.name[2:])}:4, {b.k.order}:4'+(' , '+', '.join(controls) if controls else '')+');')
    atom=b.k.operand_types['sz'];en=b.g.arch.enums[atom.type];sv=b.values['sz'].symbol
    if load:result=b.local(b.outputs['Rd'].size,'0')
    else:result=b.values['Rb']
    supported=[]
    for label,width,signed in [('U8',1,False),('S8',1,True),('U16',2,False),('S16',2,True),('32',4,False),('64',8,False),('128',16,False)]:
        if label not in en or width>result.size:continue
        supported.append(en[label]);skip='mem'+str(width)+('s' if signed else 'u')
        b.body.append(f'if ({sv} != {en[label]}) goto <{skip}>;')
        if load:
            v=b.local(width,f'*[{space}]:{width} {addr.symbol}')
            expr=v.symbol if width==result.size else f'{"sext" if signed else "zext"}({v.symbol})'
            b.body.append(f'{result.symbol} = {expr};')
        else:
            value=result.symbol if result.size==width else b.local(width,f'{result.symbol}:{width}').symbol
            b.body.append(f'*[{space}]:{width} {addr.symbol} = {value};')
        b.body += ['goto <memdone>;',f'<{skip}>']
    b.body += ['goto <opaque>;','<memdone>']
    if load:
        b.body.append(f'{b.outputs["Rd"].symbol} = {result.symbol};')
        for name,out in b.outputs.items():
            if name!='Rd':b.body.append(f'{out.symbol} = 0;')

for _op in ('LDG','STG','LDS','STS','LDL','STL','LDC'):
    globals()['emit_'+_op]=lambda b,check=False,op=_op:memory(b,op,check)
