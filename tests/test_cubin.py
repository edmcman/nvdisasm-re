"""CUDA parameter metadata and whole-kernel launch setup regressions."""
import struct
import pytest
from sass.decode import cubin


def record(attribute,payload):
    return struct.pack('<BBH',4,attribute,len(payload))+payload


def parameter(ordinal,offset,size):
    return record(0x17,struct.pack('<IHHI',0,ordinal,offset,size<<18))


def image(monkeypatch,metadata):
    monkeypatch.setattr(cubin,'sections',lambda _: {
        '.text.kernel':b'code', '.nv.info.kernel':metadata,
        '.nv.constant0.kernel':bytes(0x180), '.nv.constant2':b'global',
        '.nv.constant0.other':b'wrong kernel'})
    monkeypatch.setattr(cubin,'elf_arch',lambda _:'SM89')
    return cubin.kernel_image(b'', 'kernel')


def test_parameter_metadata(monkeypatch):
    # Records arrive in reverse ordinal order; offsets come from the cubin.
    metadata=record(0x0a,struct.pack('<IHH',7,0x160,24))
    metadata+=parameter(1,16,8)+parameter(0,0,8)
    result=image(monkeypatch,metadata)
    assert result['parameter_base']==0x160
    assert result['parameters']==[dict(offset=0,size=8),dict(offset=16,size=8)]
    assert result['constants']=={0:bytes(0x180),2:b'global'}


@pytest.mark.parametrize('body',[b'\x04',b'\x04\x17\x0c\x00\x00',b'\x05\x00\x00\x00'])
def test_bad_attribute_records(body):
    with pytest.raises(ValueError):list(cubin.info_records(body))


@pytest.mark.parametrize('params',[parameter(1,0,8),parameter(0,12,8)])
def test_bad_parameter_layout(monkeypatch,params):
    with pytest.raises(ValueError):
        image(monkeypatch,record(0x0a,struct.pack('<IHH',7,0x160,16))+params)


def test_inline_attribute_does_not_consume_following_record():
    assert list(cubin.info_records(struct.pack('<BBH',3,0x19,16)+record(0x37,b'abcd')))==[
        (0x19,b'\x10\x00'),(0x37,b'abcd')]


@pytest.mark.parametrize('sm,arch',[(89,'SM89'),(87,'SM86'),(110,'SM101'),(121,'SM120')])
def test_elf_arch_aliases(sm,arch):
    header=bytearray(0x40);header[8]=8;struct.pack_into('<I',header,0x30,sm<<8)
    assert cubin.elf_arch(bytes(header))==arch


@pytest.mark.parametrize('arch,dims,stack',[('SM89',0x0,0x28),('SM90',0x0,0x28),('SM100',0x360,0x37c)])
def test_launch_layout(arch,dims,stack):
    from sass import launch
    image=dict(name='k',arch=arch,code=b'',constants={0:bytes(0x400)},parameter_base=0x380,parameter_size=8,parameters=[dict(offset=0,size=8)])
    bank=bytes.fromhex(launch.kernel_request(image,[0x1234],[],64,64,{})['memory'][0]['hex'])
    assert struct.unpack_from('<III',bank,dims)==(64,1,1) and struct.unpack_from('<I',bank,stack)==(0x1000,)
    assert struct.unpack_from('<Q',bank,0x380)==(0x1234,)


def test_launch_layout_unknown():
    from sass import launch
    with pytest.raises(ValueError,match='SM107'):
        launch.kernel_request(dict(name='k',arch='SM107',code=b'',constants={},parameter_base=0,parameter_size=0,parameters=[]),[],[],1,1,{})
