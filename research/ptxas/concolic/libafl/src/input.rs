//! PTX file whose mutable part is the instruction region between marker comments.
use std::{ops::RangeBounds, path::Path, vec};
use libafl::{Error, inputs::{HasMutatorBytes, HasTargetBytes, Input, ResizableMutator}};
use libafl_bolts::{HasLen, fs::write_file_atomic, ownedref::OwnedSlice};

/// Byte-level mutators see only `region`; ptxas sees `prefix ++ region ++ suffix`.
#[derive(Clone, Debug, Hash, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct PtxInput { prefix: Vec<u8>, pub region: Vec<u8>, suffix: Vec<u8> }

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Span of the single-line `//` or `/* */` comment containing `marker`.
fn comment(data: &[u8], marker: &[u8]) -> Option<(usize, usize)> {
    let at = find(data, marker)?;
    let line = data[..at].iter().rposition(|&b| b == b'\n').map_or(0, |i| i + 1);
    let opener = |o: &[u8]| data[line..at].windows(2).rposition(|w| w == o).map(|i| line + i);
    match (opener(b"/*"), opener(b"//")) {
        (Some(b), l) if l.is_none_or(|l| b > l) => Some((b, at + find(&data[at..], b"*/")? + 2)),
        (_, Some(l)) => Some((l, data[at..].iter().position(|&b| b == b'\n').map_or(data.len(), |i| at + i + 1))),
        _ => None,
    }
}

impl PtxInput {
    /// Region = bytes after the BEGIN_INSTRUCTION comment up to the END_INSTRUCTION comment.
    pub fn parse(data: &[u8]) -> Option<Self> {
        let (_, begin) = comment(data, b"BEGIN_INSTRUCTION")?;
        let (end, _) = comment(data, b"END_INSTRUCTION")?;
        (begin <= end).then(|| Self { prefix: data[..begin].to_vec(), region: data[begin..end].to_vec(),
                                      suffix: data[end..].to_vec() })
    }
    /// [begin, end) of the region in the file; the concolic worker uses the same span.
    pub fn span(&self) -> (usize, usize) { (self.prefix.len(), self.prefix.len() + self.region.len()) }
    pub fn bytes(&self) -> Vec<u8> { [&self.prefix[..], &self.region, &self.suffix].concat() }
}

impl Input for PtxInput {
    fn to_file<P: AsRef<Path>>(&self, path: P) -> Result<(), Error> { write_file_atomic(path, &self.bytes()) }
    fn from_file<P: AsRef<Path>>(path: P) -> Result<Self, Error> {
        Self::parse(&std::fs::read(path)?).ok_or_else(|| Error::illegal_argument("no instruction markers"))
    }
}

impl HasTargetBytes for PtxInput {
    fn target_bytes(&self) -> OwnedSlice<'_, u8> { OwnedSlice::from(self.bytes()) }
}
impl HasLen for PtxInput { fn len(&self) -> usize { self.region.len() } }
impl HasMutatorBytes for PtxInput {
    fn mutator_bytes(&self) -> &[u8] { &self.region }
    fn mutator_bytes_mut(&mut self) -> &mut [u8] { &mut self.region }
}
impl ResizableMutator<u8> for PtxInput {
    fn resize(&mut self, new_len: usize, value: u8) { self.region.resize(new_len, value) }
    fn extend<'a, I: IntoIterator<Item = &'a u8>>(&mut self, iter: I) { Extend::extend(&mut self.region, iter.into_iter().copied()) }
    fn splice<R: RangeBounds<usize>, I: IntoIterator<Item = u8>>(&mut self, range: R, replace_with: I) -> vec::Splice<'_, I::IntoIter> {
        self.region.splice(range, replace_with)
    }
    fn drain<R: RangeBounds<usize>>(&mut self, range: R) -> vec::Drain<'_, u8> { self.region.drain(range) }
}

#[cfg(test)]
mod tests {
    use super::*;
    const LINE: &[u8] = b"head\n// BEGIN_INSTRUCTION\nadd.u32 %r2, %r0, %r1;\n// END_INSTRUCTION\ntail\n";
    const INLINE: &[u8] = b"ld; /* BEGIN_INSTRUCTION */ add.s32 %r2, %r0, %r1; /* END_INSTRUCTION */ st;\n";

    #[test]
    fn markers() {
        let p = PtxInput::parse(LINE).unwrap();
        assert_eq!(p.region, b"add.u32 %r2, %r0, %r1;\n");
        assert_eq!(p.bytes(), LINE);
        let p = PtxInput::parse(INLINE).unwrap();
        assert_eq!(p.region, b" add.s32 %r2, %r0, %r1; ");
        assert_eq!(p.bytes(), INLINE);
        assert!(PtxInput::parse(b"add.u32 %r2, %r0, %r1;").is_none());
        assert!(PtxInput::parse(b"/* END_INSTRUCTION */ x /* BEGIN_INSTRUCTION */").is_none());
    }

    #[test]
    fn havoc_keeps_scaffold() {
        use libafl::{corpus::{Corpus, InMemoryCorpus, Testcase}, feedbacks::ConstFeedback,
                     mutators::{HavocScheduledMutator, Mutator, havoc_mutations}, state::StdState};
        use libafl_bolts::rands::StdRand;
        let seed = PtxInput::parse(LINE).unwrap();
        let mut corpus = InMemoryCorpus::new();
        corpus.add(Testcase::new(PtxInput::parse(INLINE).unwrap())).unwrap();  // crossover donor
        let mut state = StdState::new(StdRand::with_seed(1), corpus, InMemoryCorpus::<PtxInput>::new(),
                                      &mut ConstFeedback::new(false), &mut ConstFeedback::new(false)).unwrap();
        let mut havoc = HavocScheduledMutator::new(havoc_mutations());
        for _ in 0..1000 {
            let mut input = seed.clone();
            havoc.mutate(&mut state, &mut input).unwrap();
            assert_eq!((&input.prefix, &input.suffix), (&seed.prefix, &seed.suffix));
        }
    }
}
