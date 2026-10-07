"""nvdisasm as a decode oracle over raw 128-bit instruction words."""
import hashlib, os, re, sqlite3, subprocess, tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

NVDISASM = os.environ.get("NVDISASM", "/tmp/.venv/lib/python3.13/site-packages/nvidia/cu13/bin/nvdisasm")
CACHE = Path(__file__).resolve().parent.parent / ".cache" / "oracle.sqlite"
BATCH = 65536
FATAL = re.compile(r"nvdisasm fatal\s*:\s*(.*?)(?: at address 0x[0-9a-f]+)?\s*$", re.M)
LINE = re.compile(r"/\*([0-9a-f]{4,})\*/\s+(.*?)\s*;")
ERR_AT = re.compile(r"nvdisasm (?:error|fatal)\s*:\s*(.*?) at address 0x([0-9a-f]+)")

@dataclass(frozen=True)
class Result:
    addr: int
    text: str | None
    error: str | None

@cache
def tool_id():
    return hashlib.sha256(Path(NVDISASM).read_bytes()).hexdigest()[:16]

def run(arch, words):
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(b"".join(words)); f.flush()
        p = subprocess.run([NVDISASM, "-b", arch, f.name], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr

def disasm_batch(arch, words):
    """Map word index -> Result. Illegal words are dropped and the rest re-run, so addresses are the final layout."""
    out, live = {}, list(range(len(words)))
    while live:
        rc, stdout, stderr = run(arch, [words[i] for i in live])
        if rc == 0:
            texts = {int(a, 16): t for a, t in LINE.findall(stdout)}
            for k, i in enumerate(live):
                out[i] = Result(16 * k, texts.get(16 * k, ""), None)  # nvdisasm prints some decodable words as blank
            break
        if "nvdisasm fatal" in stderr:
            # A fatal error aborts the batch and its reported address is unreliable: bisect down to the culprit.
            if len(live) == 1:
                out[live[0]] = Result(0, None, FATAL.search(stderr)[1]); break
            for part in (live[:len(live) // 2], live[len(live) // 2:]):
                out |= {part[j]: r for j, r in disasm_batch(arch, [words[i] for i in part]).items()}
            break
        bad = {int(a, 16) // 16: msg for msg, a in ERR_AT.findall(stderr)}
        if not bad:
            if len(live) == 1:
                out[live[0]] = Result(0, None, stderr.strip()); break
            raise RuntimeError(f"nvdisasm failed without addresses: {stderr[:500]}")
        for k, msg in bad.items():
            out[live[k]] = Result(16 * k, None, msg)
        live = [i for k, i in enumerate(live) if k not in bad]
    return out

class Oracle:
    def __init__(self, path=CACHE):
        path.parent.mkdir(exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("create table if not exists r (tool text, arch text, word blob, addr int, text text, error text, primary key (tool, arch, word))")

    def disasm(self, arch, words):
        """Results for unique words; each is decoded at the address it got in its batch."""
        words = list(dict.fromkeys(words))
        known = self._lookup(arch, words)
        todo = [w for w in words if w not in known]
        for s in range(0, len(todo), BATCH):
            chunk = todo[s:s + BATCH]
            res = disasm_batch(arch, chunk)
            rows = [(tool_id(), arch, chunk[i], r.addr, r.text, r.error) for i, r in res.items()]
            self.db.executemany("insert or replace into r values (?,?,?,?,?,?)", rows)
            self.db.commit()
            known |= {chunk[i]: r for i, r in res.items()}
        return {w: known[w] for w in words}

    def _lookup(self, arch, words):
        q = "select word, addr, text, error from r where tool=? and arch=? and word=?"
        return {w: Result(*row[1:]) for w in words if (row := self.db.execute(q, (tool_id(), arch, w)).fetchone())}
