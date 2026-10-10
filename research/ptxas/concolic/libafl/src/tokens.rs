//! One-token substitutions within parser token classes. Token spans come from the pinned
//! ptxas lexer automaton, read from the compiler (`lexer_vocab.py` documents the layout).
//! Reserved tokens are replaced by the other spellings returning the same parser token;
//! identifiers only at statement heads, by registered opcode names. Other identifiers,
//! numbers and punctuation are never touched.
use std::{borrow::Cow, collections::HashMap, ops::Range, path::Path, rc::Rc};
use libafl::{Error, HasMetadata, corpus::CorpusId, mutators::MultiMutator};
use libafl_bolts::Named;
use crate::{State, input::PtxInput};

const START_STATES: u64 = 0x203c020;
const IDENTIFIER: u32 = 528;
const COMMENTS: [(u32, &[u8]); 2] = [(545, b"\n"), (544, b"*/")];
const SKIP: [u32; 4] = [543, 544, 545, 546]; // whitespace, comments, newline
const NONE: u16 = u16::MAX;
pub const OPCODE: usize = 0;

fn le(bytes: &[u8]) -> u64 { bytes.iter().rev().fold(0, |value, &b| value << 8 | b as u64) }

/// Loadable-segment reads by virtual address.
fn reader<'a>(elf: &'a [u8]) -> impl Fn(u64, usize) -> Option<&'a [u8]> {
    let (phoff, size, count) = (le(&elf[32..40]) as usize, le(&elf[54..56]) as usize, le(&elf[56..58]) as usize);
    let segments: Vec<_> = (0..count).map(|i| &elf[phoff + i * size..]).filter(|h| le(&h[..4]) == 1)
        .map(|h| (le(&h[8..16]) as usize, le(&h[16..24]), le(&h[32..40]))).collect();
    move |address, n| segments.iter().find(|&&(_, vaddr, len)| vaddr <= address && address + n as u64 <= vaddr + len)
        .map(|&(offset, vaddr, _)| { let at = offset + (address - vaddr) as usize; &elf[at..at + n] })
}

fn find(data: &[u8], needle: &[u8]) -> Option<usize> { data.windows(needle.len()).position(|w| w == needle) }

/// flex -F table: entry {u32 check, i32 next} at state+8*c, accept action u32 at state-4.
pub struct Lexer { accept: Vec<u32>, next: Vec<[u16; 128]> }
impl Lexer {
    pub fn load(compiler: &Path) -> Self {
        let elf = std::fs::read(compiler).unwrap();
        let read = reader(&elf);
        let start = le(read(START_STATES + 8, 8).unwrap());
        let (mut index, mut order, mut next) = (HashMap::from([(start, 0)]), vec![start], Vec::new());
        while next.len() < order.len() {
            let state = order[next.len()]; let mut row = [NONE; 128];
            for c in 1..128u64 {
                let Some(entry) = read(state + 8 * c, 8) else { continue };
                if le(&entry[..4]) != c { continue; }
                let target = state.wrapping_add_signed(le(&entry[4..]) as u32 as i32 as i64 * 8);
                let fresh = order.len() as u16;
                row[c as usize] = *index.entry(target).or_insert_with(|| { order.push(target); fresh });
            }
            next.push(row);
        }
        let accept = order.iter().map(|&s| le(read(s - 4, 4).unwrap()) as u32).collect();
        Self { accept, next }
    }

    /// Longest match with backup, as flex; comment actions consume their comment.
    /// Unmatched bytes are single tokens with action 0.
    pub fn lex(&self, data: &[u8]) -> Vec<(Range<usize>, u32)> {
        let (mut tokens, mut at) = (Vec::new(), 0);
        while at < data.len() {
            let (mut state, mut i, mut last) = (0, at, None);
            while let Some(&n) = data.get(i).filter(|&&c| c < 128).map(|&c| &self.next[state][c as usize]) {
                if n == NONE { break; }
                state = n as usize; i += 1;
                if self.accept[state] != 0 { last = Some((i, self.accept[state])); }
            }
            let (end, action) = last.unwrap_or((at + 1, 0));
            tokens.push((at..end, action));
            at = COMMENTS.iter().find(|(a, _)| *a == action)
                .map_or(end, |(_, close)| find(&data[end..], close).map_or(data.len(), |n| end + n + close.len()));
        }
        tokens
    }
}

#[derive(Debug, Default, Clone, serde::Serialize, serde::Deserialize)]
pub struct Stats { pub attempts: u64, pub slots: u64, pub candidates: u64, pub admitted: u64 }
libafl_bolts::impl_serdeany!(Stats);

/// Class 0 holds registered opcode names; the others are reserved-token classes keyed by
/// the parser token their lexer actions return.
pub struct Vocabulary { lexer: Lexer, class_of: HashMap<u32, usize>, pub labels: Vec<String>, classes: Vec<Vec<Vec<u8>>> }
impl Vocabulary {
    pub fn load(compiler: &Path) -> Self {
        let vocab: serde_json::Value = serde_json::from_str(include_str!("../../lexer_vocab.json")).unwrap();
        let registry: serde_json::Value = serde_json::from_str(include_str!("../../opcode_registry.json")).unwrap();
        let strings = |list: &serde_json::Value| list.as_array().unwrap().iter().map(|s| s.as_str().unwrap().as_bytes().to_vec()).collect::<Vec<_>>();
        let (mut labels, mut classes, mut class_of) = (vec!["opcode".to_owned()], vec![strings(&registry["names"])], HashMap::new());
        for (token, actions) in vocab["classes"].as_object().unwrap() {
            class_of.extend(actions.as_array().unwrap().iter().map(|a| (a["action"].as_u64().unwrap() as u32, classes.len())));
            classes.push(actions.as_array().unwrap().iter().flat_map(|a| strings(&a["strings"])).collect());
            labels.push(token.clone());
        }
        let this = Self { lexer: Lexer::load(compiler), class_of, labels, classes };
        // Every spelling must lex alone to an action of its own class, or the table was misread.
        for (class, members) in this.classes.iter().enumerate().skip(1) {
            for member in members {
                let tokens = this.lexer.lex(member);
                assert!(tokens.len() == 1 && this.class_of.get(&tokens[0].1) == Some(&class), "lexer table disagrees on {:?}", String::from_utf8_lossy(member));
            }
        }
        this
    }

    /// Substitutable tokens: reserved tokens of a known class, and identifiers at statement
    /// heads (after `;`, `{`, `}`, a label or a guard, and not themselves a label).
    pub fn slots(&self, data: &[u8]) -> Vec<(Range<usize>, usize)> {
        let tokens: Vec<_> = self.lexer.lex(data).into_iter().filter(|(_, a)| !SKIP.contains(a)).collect();
        let text = |i: usize| &data[tokens[i].0.clone()];
        (0..tokens.len()).filter_map(|i| {
            let (range, action) = tokens[i].clone();
            if action != IDENTIFIER { return self.class_of.get(&action).map(|&class| (range, class)); }
            let after = i == 0 || matches!(text(i - 1), [b';' | b'{' | b'}' | b':']);
            let guarded = i >= 2 && (text(i - 2) == b"@" && text(i - 1) != b"!" || i >= 3 && text(i - 2) == b"!" && text(i - 3) == b"@");
            let label = i + 1 < tokens.len() && text(i + 1) == b":";
            ((after || guarded) && !label).then_some((range, OPCODE))
        }).collect()
    }

    pub fn candidates(&self, region: &[u8]) -> Vec<(usize, Vec<u8>)> {
        self.slots(region).into_iter().flat_map(|(range, class)| {
            let (before, current, after) = (&region[..range.start], &region[range.clone()], &region[range.end..]);
            self.classes[class].iter().filter(move |member| member[..] != *current)
                .map(move |member| (class, [before, member, after].concat()))
        }).collect()
    }
}

/// Loaded once per worker; each epoch's stage shares it.
pub struct TokenSubstitution(pub Rc<Vocabulary>);
impl Named for TokenSubstitution {
    fn name(&self) -> &Cow<'static, str> { static NAME: Cow<'static, str> = Cow::Borrowed("token_substitution"); &NAME }
}
impl MultiMutator<PtxInput, State> for TokenSubstitution {
    fn multi_mutate(&mut self, state: &mut State, input: &PtxInput, max: Option<usize>) -> Result<Vec<PtxInput>, Error> {
        if crate::STOP.load(std::sync::atomic::Ordering::Relaxed) { return Ok(Vec::new()); }
        let slots = self.0.slots(&input.region).len();
        let candidates: Vec<_> = self.0.candidates(&input.region).into_iter().take(max.unwrap_or(usize::MAX))
            .map(|(_, region)| input.with_region(region)).collect();
        let stats = state.metadata_or_insert_with(Stats::default);
        stats.attempts += 1; stats.slots += slots as u64; stats.candidates += candidates.len() as u64;
        Ok(candidates)
    }
    fn multi_post_exec(&mut self, state: &mut State, id: Option<CorpusId>) -> Result<(), Error> {
        if id.is_some() { state.metadata_or_insert_with(Stats::default).admitted += 1; }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const PTXAS: &str = "/usr/local/cuda-13.0/bin/ptxas";

    #[test]
    #[ignore = "requires pinned ptxas"]
    fn slots_follow_the_lexer() {
        let t = Vocabulary::load(Path::new(PTXAS));
        let input = b"L1: @!%p0 mad.wide.u32 %rd2, %r0, %r1; // add.rn\n{ add.rn.f32 %f1, L1; } /* sub.u32; */ bra L1;";
        let slots: Vec<_> = t.slots(input).into_iter().map(|(r, c)| (std::str::from_utf8(&input[r]).unwrap(), t.labels[c].clone())).collect();
        let expect: Vec<(&str, String)> = vec![("mad.wide", "opcode".into()), (".u32", "275".into()), ("add", "opcode".into()),
            (".rn", "288".into()), (".f32", "275".into()), ("bra", "opcode".into())];
        assert_eq!(slots, expect);
        let candidates = t.candidates(b"add.rn.f32 %f1, %f2;");
        assert!(candidates.iter().any(|(_, c)| c == b"mad.wide.rn.f32 %f1, %f2;"));
        assert!(candidates.iter().any(|(_, c)| c == b"add.rz.f32 %f1, %f2;"));
        assert!(candidates.iter().any(|(_, c)| c == b"add.rn.f64 %f1, %f2;"));
        assert!(candidates.iter().all(|(_, c)| c.ends_with(b" %f1, %f2;")));
    }

    /// The Python lexer is checked against ptxas under GDB (lexer_vocab_check.py).
    #[test]
    #[ignore = "requires pinned ptxas and python3"]
    fn lexer_matches_python() {
        let lexer = Lexer::load(Path::new(PTXAS));
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
        let mut files: Vec<_> = std::fs::read_dir(dir.join("..")).unwrap().map(|e| e.unwrap().path())
            .filter(|p| p.extension().is_some_and(|e| e == "ptx")).collect();
        files.extend([dir.join("generic_sm75.ptx"), dir.join("instruction.ptx")]);
        for file in files {
            let ours: Vec<_> = lexer.lex(&std::fs::read(&file).unwrap()).into_iter().map(|(r, a)| (r.start, r.end, a)).collect();
            let output = std::process::Command::new("python3").current_dir(&dir).args(["-c",
                "import json,sys; from lexer_vocab import automaton,lex; from pinned import loader; \
                 print(json.dumps([[a,b,x or 0] for a,b,x in lex(automaton(loader(sys.argv[1])),open(sys.argv[2],'rb').read())]))",
                PTXAS, file.to_str().unwrap()]).output().unwrap();
            let python: Vec<(usize, usize, u32)> = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(ours, python, "{}", file.display());
        }
    }
}
