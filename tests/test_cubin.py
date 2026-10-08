"""CUDA parameter metadata and whole-kernel launch setup regressions."""
import struct
import pytest
from sass import cubin


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
