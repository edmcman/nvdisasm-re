"""PTX token identity and SASS code identity for generation campaigns."""
import hashlib
import json
import re
import struct

# Preserve quoted strings and token boundaries; omit comments and whitespace (ptxas also
# skips \x1a, ASCII SUB, as a separator).
# Never rewrite the source passed to ptxas or SymQEMU.
TOKEN = re.compile(
    rb'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    rb'//[^\r\n]*|/\*[\s\S]*?\*/|[\s\x1a]+|'
    rb'\.?[A-Za-z_$%][A-Za-z0-9_$%]*|'
    rb'0[xXfFdD][0-9A-Fa-f]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|'
    rb'\.\d+(?:[eE][+-]?\d+)?|[\s\S]'
)


NAME = re.compile(rb"[A-Za-z_$%][A-Za-z0-9_$%]*\Z")


def tokens(data):
    return [m.group() for m in TOKEN.finditer(data)
            if m.group().strip(b" \t\r\n\v\f\x1a") and not m.group().startswith((b"//", b"/*"))]


def digest(tokens):
    return hashlib.sha256(json.dumps([t.hex() for t in tokens], separators=(",", ":")).encode()).hexdigest()


def token_key(data):
    return digest(tokens(data))


def heads(t):
    """Instruction mnemonics: first name of a statement, after an optional guard; not label definitions."""
    for i, tok in enumerate(t):
        prev = t[i - 1] if i else b";"
        guarded = i >= 2 and (t[i - 2] == b"@" or t[i - 2:i - 1] == [b"!"] and i >= 3 and t[i - 3] == b"@")
        if (prev in (b";", b"{", b"}", b":") or guarded) and t[i + 1:i + 2] != [b":"]:
            yield tok


def name_key(data):
    """token_key modulo consistent renaming of names the file binds: declarations following a
    directive or type token (.entry k, .param .u32 a, .reg .b32 %r<16>) and labels. A bound name
    that also appears as a mnemonic is kept, so renaming never merges different instructions."""
    t = tokens(data)
    bound = [b for a, b, c in zip([b";"] + t, t, t[1:] + [b";"])
             if NAME.match(b) and (a.startswith(b".") and a != b".target" or c == b":")]
    bound = dict.fromkeys(bound).keys() - set(heads(t))
    families = {b for b, c in zip(t, t[1:]) if b in bound and c == b"<"}
    names = {}
    def rename(tok):
        family = next((f for f in families if tok.startswith(f) and tok[len(f):].isdigit()), None)
        if family: return names.setdefault(family, b"\0%d" % len(names)) + tok[len(family):]
        return names.setdefault(tok, b"\0%d" % len(names)) if tok in bound else tok
    return digest([rename(tok) for tok in t])


def comment(data, marker):
    """Span of the single-line // or /* */ comment containing marker."""
    at = data.find(marker)
    if at < 0: return None
    line = data.rfind(b"\n", 0, at) + 1
    block, slash = data.rfind(b"/*", line, at), data.rfind(b"//", line, at)
    if block > slash:
        close = data.find(b"*/", at)
        return None if close < 0 else (block, close + 2)
    if slash >= 0:
        newline = data.find(b"\n", at)
        return slash, len(data) if newline < 0 else newline + 1
    return None


def instruction_region(data):
    """[begin, end) between the BEGIN_INSTRUCTION and END_INSTRUCTION comments, as libafl/src/input.rs."""
    begin, end = comment(data, b"BEGIN_INSTRUCTION"), comment(data, b"END_INSTRUCTION")
    return (begin[1], end[0]) if begin and end and begin[1] <= end[0] else None


def sass_key(cubin):
    """Hash executable sections of a CUDA ELF64 cubin, excluding metadata."""
    if cubin[:6] != b"\x7fELF\x02\x01":
        raise ValueError("expected a little-endian ELF64 cubin")
    table = struct.unpack_from("<Q", cubin, 40)[0]
    stride, count = struct.unpack_from("<HH", cubin, 58)
    if stride < 64:
        raise ValueError("invalid ELF section header size")
    if count == 0:
        count = struct.unpack_from("<Q", cubin, table + 32)[0]
    sections = []
    for index in range(count):
        entry = table + index * stride
        _, kind, flags, _, offset, size, *_ = struct.unpack_from("<IIQQQQIIQQ", cubin, entry)
        if kind == 1 and flags & 4:
            if offset + size > len(cubin):
                raise ValueError("truncated executable section")
            sections.append(cubin[offset:offset + size])
    if not sections:
        return None
    digest = hashlib.sha256()
    for code in sorted(sections):
        digest.update(struct.pack("<Q", len(code)))
        digest.update(code)
    return digest.hexdigest()
