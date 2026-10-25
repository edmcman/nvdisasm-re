"""The CPU runtime of the pcodeops the languages call: instruction primitives
(src/sass/semantics/primitive.py), whose values the C++ primitives compute from this module's
parsed context (sass.emulate.native), and the environment's operands and memory controls.

Unknown operations fail explicitly. This is single-thread/explicit-warp context,
not an implementation of CUDA's warp scheduler or memory concurrency model.
"""
from sass.decode import ir
from sass.semantics.bitvec import PRIMITIVE_OP
from sass.emulate import native


class UnsupportedSemantics(NotImplementedError): pass


SPACES = tuple(native.SPACES)
SYNCHRONIZATION = ('bar','bssy','bsync','break','warpsync','bmov')


def memory_controls(arch,ordinal,controls):
    """The class of a memory access with these encoded controls (sem, sco, cop, cop2, private);
    raises on an INVALID label."""
    k=arch.classes[ordinal]
    for key,value in zip((n for n in ('sem','sco','cop','cop2','private') if n in k.operand_types),controls):
        atom=k.operand_types[key];label=arch.rev_enums[atom.type].get(value,'INVALID')
        if label.startswith('INVALID'):raise UnsupportedSemantics('memory control '+label)
    return k


class Runtime:
    def __init__(self, sm, context=None, handlers=None):
        self.sm=sm;self.handlers=handlers or {};self.context=context or {};self.events=[]

    def call(self,name,args,size):
        """The pcodeop `name` on integer args, a size-byte result (0: an effect)."""
        if name in self.handlers:return self.handlers[name](args,size,self.context)
        if name.startswith(PRIMITIVE_OP):return self.primitive(name[len(PRIMITIVE_OP):],args,size)
        if name.startswith('sass_opaque_'):
            raise UnsupportedSemantics(f'{name}{tuple(args)}')
        arch=ir.load('SM'+str(self.sm))
        if name=='sass_operand_value':
            ordinal,index,*children=args;k=arch.classes[ordinal]
            atom=list(k.operand_types.values())[index]
            # Stateful operands are provided by the execution environment rather
            # than given a made-up bank/descriptor layout.
            supplied=self.context.get('operands',{})
            key=k.name+'.'+atom.name
            if key not in supplied:raise UnsupportedSemantics('operand '+key)
            return int(supplied[key])
        if name=='sass_memory_order':
            ordinal,*controls=args
            self.events.append(dict(op=memory_controls(arch,ordinal,controls).mnemonic,controls=controls));return 0
        if name=='sass_memory_descriptor':
            try:native.memory_descriptor(*args)
            except native.Failure as error:raise UnsupportedSemantics(str(error)) from error
            return 0
        raise UnsupportedSemantics(name)

    def primitive(self,name,args,size):
        """Context parsing; the values come from the C++ primitives (sass/emulate/include/sass/primitives.hpp)."""
        family=name.partition('_')[0]
        try:
            match name,args:
                case 'lane',[]:return self.warp()[1]
                case 'active',[]:return self.warp()[2]
                case 'waiting',[]:
                    # The lanes of this warp still alive but not executing this instruction (BRA.DIV/.CONV).
                    waiting=self.context.get('warp',{}).get('waiting')
                    if waiting is None:raise UnsupportedSemantics('warp waiting-lane mask required')
                    return int(waiting)&0xffffffff
                case 'read_lane',[x,src]:return native.primitive(name,[self.lanes('values',x),src],*self.warp()[1:])
                case 'ballot',[p]:return native.primitive(name,[self.lanes('predicates',p)],*self.warp()[1:])
                case 'match'|'redux_and'|'redux_or'|'redux_xor'|'redux_add'|'redux_umin'|'redux_umax'|'redux_smin'|'redux_smax',[x]:
                    return native.primitive(name,[self.lanes('values',x)],*self.warp()[1:])
                case 'address_space',[address]:return native.primitive(name,[address],ranges=self.address_ranges())
                case _ if family=='mufu':return native.primitive(name,args)
                case _,[*inputs] if name in SYNCHRONIZATION:return self.synchronize(name,inputs,read=size>0)
        except native.Failure as error:raise UnsupportedSemantics(str(error)) from error
        raise UnsupportedSemantics('primitive '+name)

    def warp(self):
        """(warp context, this lane, executing lanes mask)."""
        warp=self.context.get('warp')
        if not warp:raise UnsupportedSemantics('explicit 32-lane warp context required')
        lane=int(warp.get('lane',0));active=int(warp.get('active',0xffffffff))
        if not 0<=lane<32 or not active>>lane&1:raise UnsupportedSemantics('warp lane is inactive or invalid')
        return warp,lane,active

    def lanes(self,key,x):
        """Every lane's contribution from the warp context's `key`, this lane's being x."""
        warp,lane,_=self.warp()
        values=warp.get(key)
        if values is None or len(values)!=32:raise UnsupportedSemantics(f'warp {key} required')
        values=[int(v) for v in values];values[lane]=x
        return values

    def address_ranges(self):
        """The context's generic address ranges, [(space, start, end)], validated."""
        ranges=self.context.get('address_spaces')
        if not isinstance(ranges,dict):raise UnsupportedSemantics('QSPC address_spaces context required')
        found=[]
        for name,intervals in ranges.items():
            if name not in SPACES or not isinstance(intervals,(list,tuple)):
                raise UnsupportedSemantics(f'malformed QSPC address ranges for {name}')
            for interval in intervals:
                if not isinstance(interval,(list,tuple)) or len(interval)!=2:
                    raise UnsupportedSemantics(f'malformed QSPC address range for {name}')
                start,end=map(int,interval)
                if not 0<=start<=end<=1<<64:raise UnsupportedSemantics(f'invalid QSPC address range for {name}')
                found.append((name,start,end))
        return found

    def synchronize(self,op,inputs,read):
        """Synchronization is supplied by the environment: a missing handler fails rather than
        silently treating reconvergence/barriers as no-ops. An event's first input enables it; a
        read (BMOV to a register) returns the handler's value for its state encoding, its first input."""
        if not read and not inputs[0]:return 0
        handler=self.context.get('synchronization',{}).get(op)
        if not handler:raise UnsupportedSemantics(op.upper()+' synchronization context required')
        self.events.append(dict(op=op.upper(),inputs=inputs))
        if not read:return 0
        # BMOV reads convergence-barrier state, which only the environment holds.
        values={str(k):v for k,v in handler.items()} if isinstance(handler,dict) else {}
        if str(inputs[0]) not in values:raise UnsupportedSemantics(op.upper()+' reduction/result requires an environment handler')
        return int(values[str(inputs[0])])
