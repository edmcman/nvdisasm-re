"""CPU implementations of named instruction primitives.

Unknown operations fail explicitly. This is single-thread/explicit-warp context,
not an implementation of CUDA's warp scheduler or memory concurrency model.
"""
import json, math, re
from fractions import Fraction
from pathlib import Path
from sass import ir, softfloat


class UnsupportedSemantics(NotImplementedError): pass


class Runtime:
    def __init__(self, language_dir, sm, context=None, handlers=None):
        self.sm=sm;self.handlers=handlers or {}
        self.language_dir=Path(language_dir);self.context=context or {};self.events=[];self.manifests={}

    def manifest(self,sm):
        if sm not in self.manifests:
            rows=json.loads((self.language_dir/f'sass_sm{sm}_coverage.json').read_text())
            self.manifests[sm]={r['cls']:r for r in rows}
        return self.manifests[sm]

    def call(self,name,args,size):
        if name in self.handlers:return self.handlers[name](args,size,self.context)
        sm=self.sm
        if name.startswith('sass_opaque_'):
            raise UnsupportedSemantics(f'{name}{tuple(args)}')
        if name=='sass_operand_value':
            ordinal,index,*children=args;k=ir.load('SM'+str(sm)).classes[ordinal]
            atom=list(k.operand_types.values())[index]
            # Stateful operands are provided by the execution environment rather
            # than given a made-up bank/descriptor layout.
            supplied=self.context.get('operands',{})
            key=k.name+'.'+atom.name
            if key not in supplied:raise UnsupportedSemantics('operand '+key)
            return int(supplied[key])
        if name=='sass_memory_order':
            ordinal,*controls=args;k=ir.load('SM'+str(sm)).classes[ordinal]
            for key,value in zip((n for n in ('sem','sco','cop','cop2','private') if n in k.operand_types),controls):
                atom=k.operand_types[key];label=ir.load('SM'+str(sm)).rev_enums[atom.type].get(value,'INVALID')
                if label.startswith('INVALID'):raise UnsupportedSemantics('memory control '+label)
            self.events.append(dict(op=k.mnemonic,controls=controls));return 0
        if not name.startswith('sass_prim_'):raise UnsupportedSemantics(name)
        ordinal,*values=args;arch=ir.load('SM'+str(sm));k=arch.classes[ordinal]
        variants=self.manifest(sm)[k.name]['variants']
        if len(variants)!=1:raise UnsupportedSemantics('primitive with modifier-dependent register spans '+k.name)
        inputs,outputs=variants[0]['inputs'],variants[0]['outputs']
        if len(values)!=len(inputs):raise UnsupportedSemantics('primitive input ABI mismatch '+k.name)
        v=dict(zip(inputs,values))
        # Multi-destination operations return a low-to-high byte bundle. All
        # inputs are evaluated before that bundle is assigned to registers.
        suffix=name[len('sass_prim_'+k.mnemonic):].lstrip('_')
        if suffix:return self.execute(arch,k,v,suffix,size)
        offset=0;result=0
        for dest,width in outputs.items():
            result|=(self.execute(arch,k,v,dest,width)&((1<<(width*8))-1))<<offset
            offset+=width*8
        if not outputs:self.execute(arch,k,v,'',0)
        return result

    def execute(self,arch,k,v,dest,size):
        op=k.mnemonic
        def label(n,default=None):
            atom=k.operand_types.get(n)
            if atom is None:return default
            out=arch.rev_enums.get(atom.type,{}).get(v[n]) if n in v else default
            if out is None or out.startswith('INVALID'):raise UnsupportedSemantics(f'{op} {n}={out}')
            return out
        def get(*names):
            for n in names:
                if n in v:return n,v[n]
            raise UnsupportedSemantics(op+' missing operand '+str(names))
        def integer(*names):
            n,x=get(*names)
            if v.get(n+'@invert'):x=~x
            if v.get(n+'@negate'):x=-x
            if v.get(n+'@not'):x=int(not x)
            return x&0xffffffff
        def floating(width,*names):
            n,x=get(*names);mask=(1<<width)-1
            if v.get(n+'@absolute'):x&=(1<<(width-1))-1
            if v.get(n+'@negate'):x^=1<<(width-1)
            return x&mask
        if op in ('FADD','FMUL','FFMA'):
            rnd=label('rnd','RN');fmz=label('fmz','nofmz');ftz=label('ftz','noftz')
            if fmz=='nofmz_hfma2':fmz='nofmz'
            if fmz not in ('nofmz','FTZ'):raise UnsupportedSemantics(op+' '+fmz)
            flush=fmz=='FTZ' or ftz=='FTZ';sat=label('sat','nosat')=='SAT'
            a=floating(32,'Ra','Sa');b=floating(32,*(('Rc','Sc','URc') if op=='FADD' else ('Rb','Sb','URb')))
            inputs=[a,b]
            if op=='FFMA':inputs.append(floating(32,'Rc','Sc','URc'))
            scale=label('scale','noscale');exponent={'noscale':0,'D2':-1,'D4':-2,'D8':-3,'M2':1,'M4':2,'M8':3}.get(scale)
            if exponent is None:raise UnsupportedSemantics(op+' scale '+scale)
            return softfloat.arithmetic({'FADD':'add','FMUL':'mul','FFMA':'fma'}[op],inputs,32,rnd,flush,sat,exponent)
        if op=='FSETP':
            flush=label('ftz','noftz')=='FTZ';a=softfloat.unpack(floating(32,'Ra','Sa'),32,flush);b=softfloat.unpack(floating(32,'Rb','Sb','URb'),32,flush)
            cmp=label('fcomp');unordered=a[0]=='nan' or b[0]=='nan'
            def numeric(x):return (-math.inf if x[1] else math.inf) if x[0]=='inf' else x[2]
            comparisons={'LT':lambda x,y:x<y,'EQ':lambda x,y:x==y,'LE':lambda x,y:x<=y,'GT':lambda x,y:x>y,'NE':lambda x,y:x!=y,'GE':lambda x,y:x>=y}
            if cmp=='NUM':result=not unordered
            elif cmp=='NAN':result=unordered
            elif cmp in ('F','T'):result=cmp=='T'
            else:
                base=cmp.removesuffix('U')
                if base not in comparisons:raise UnsupportedSemantics('FCMP '+cmp)
                result=(unordered and cmp.endswith('U')) or (not unordered and comparisons[base](numeric(a),numeric(b)))
            if dest=='Pv':result=not result
            pred=bool(integer('Pp'));bop=label('bop','AND')
            return int({'AND':lambda:result and pred,'OR':lambda:result or pred,'XOR':lambda:result!=pred}[bop]())
        if op in ('F2I','I2F','I2FP'):
            src=label('srcfmt');dst=label('dstfmt');n,bits=get('Sb','Rb','Ra','Sa')
            if not src or not dst:raise UnsupportedSemantics(op+' format')
            match=lambda s:re.fullmatch(r'([FUS])(8|16|32|64)',s)
            sf,df=match(src),match(dst)
            if not sf or not df:raise UnsupportedSemantics(op+' format '+src+'/'+dst)
            sw,dw=int(sf[2]),int(df[2]);shift=0
            if sw==16:shift=16 if label('hsel','H0')=='H1' else 0
            if sw==8:shift=int(label('b3b0','B0')[1:])*8
            bits=bits>>shift&((1<<sw)-1)
            rnd=label('rnd','RN');rnd={'ROUND':'RN','FLOOR':'RM','CEIL':'RP','TRUNC':'RZ'}.get(rnd,rnd)
            if op=='F2I':
                ntz=label('ntz','nontz')
                if v.get(n+'@absolute'):bits&=(1<<(sw-1))-1
                if v.get(n+'@negate'):bits^=1<<(sw-1)
                ftz=label('ftz','noftz')=='FTZ'
                if ntz=='NTZ':
                    # Measured SM89 F2I.TRUNC.NTZ: NaNs become zero, infinities
                    # and finite overflow clamp to the signed destination range.
                    if (arch.name,sw,dw,df[1],rnd)!=('SM89',32,32,'S','RZ'):
                        raise UnsupportedSemantics('unverified NTZ conversion')
                    kind,sign,value=softfloat.unpack(bits,sw,ftz)
                    lo,hi=-(1<<31),(1<<31)-1
                    result=0 if kind=='nan' else (lo if sign else hi) if kind=='inf' else max(lo,min(hi,softfloat.round_integer(value,rnd)))
                elif ntz in ('nontz','NOTZ','noNTZ'):
                    result=softfloat.float_to_int(bits,sw,dw,df[1]=='S',rnd,ftz)
                else:raise UnsupportedSemantics('NTZ conversion '+ntz)
                if df[1]=='S' and dw<size*8 and result>>(dw-1):result-=1<<dw
            else:
                if sf[1]=='S' and bits>>(sw-1):bits-=1<<sw
                if v.get(n+'@negate'):bits=-bits
                if v.get(n+'@absolute'):bits=abs(bits)
                result=softfloat.pack(Fraction(bits),dw,rnd)
            return result&((1<<(size*8))-1)
        if op=='PRMT':
            mode=label('pmode','IDX')
            a=integer('Ra');selector=integer('Rb','Sb');b=integer('Rc','Sc');data=a|(b<<32);out=0
            for i in range(4):
                pick=selector>>(i*4)&15 if mode=='IDX' else {'F4E':lambda:(selector&3)+i, 'B4E':lambda:((selector&3)-i)&7, 'RC8':lambda:selector&3, 'RC16':lambda:((selector&1)*2)+(i&1), 'ECL':lambda:max(i,selector&3), 'ECR':lambda:min(i,selector&3)}[mode]()
                byte=data>>((pick&7)*8)&255
                if mode=='IDX' and pick&8:byte=255 if byte&128 else 0
                out|=byte<<(i*8)
            return out
        if op in ('SHFL','VOTE'):
            warp=self.context.get('warp')
            if not warp or len(warp.get('values',[]))!=32:raise UnsupportedSemantics('explicit 32-lane warp context required')
            lane=int(warp.get('lane',0));active=int(warp.get('active',0xffffffff))
            if not 0<=lane<32 or not active>>lane&1:raise UnsupportedSemantics('warp lane is inactive or invalid')
            if op=='VOTE':
                preds=warp.get('predicates')
                if preds is None or len(preds)!=32:raise UnsupportedSemantics('warp predicates required')
                if v.get('Pp@not'):preds=[not x for x in preds]
                mask=sum((1<<i) for i in range(32) if active>>i&1 and preds[i])
                mode=label('voteop');result={'ALL':mask==active,'ANY':bool(mask),'EQ':mask==0 or mask==active}[mode]
                return mask if dest=='Rd' else int(result)
            b=integer('Rb','Sb')&31;c=integer('Rc','Sc');clamp=c&31;segment=c>>8&31
            minimum=lane&segment;maximum=(lane&segment)|(clamp&~segment&31);mode=label('shflmd')
            target={'IDX':minimum|(b&~segment&31),'UP':lane-b,'DOWN':lane+b,'BFLY':lane^b}[mode]
            valid=minimum<=target<=maximum if 0<=target<32 else False
            if valid and not active>>target&1:raise UnsupportedSemantics('inactive shuffle source has undefined value')
            return int(valid) if dest=='Pu' else int(warp['values'][target if valid else lane])
        if op in ('BAR','BSSY','BSYNC'):
            # Synchronization is supplied by the environment. A missing handler
            # fails rather than silently treating reconvergence/barriers as no-ops.
            if 'Pp' in v and not integer('Pp'):return 0
            if dest:raise UnsupportedSemantics(op+' reduction/result requires an environment handler')
            key=op.lower()
            if not self.context.get('synchronization',{}).get(key):raise UnsupportedSemantics(op+' synchronization context required')
            self.events.append(dict(op=op,values=v));return 0
        if op=='MUFU':raise UnsupportedSemantics('MUFU approximation requires a hardware-specific implementation')
        raise UnsupportedSemantics('primitive '+op)
