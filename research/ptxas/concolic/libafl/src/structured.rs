//! Havoc mutators that edit whole PTX tokens, so one stacked mutation changes an opcode, a
//! suffix or an operand list rather than bytes: `TokenSwap` replaces a lexer token by another
//! of its parser-token class; `OperandEdit` inserts, drops, duplicates or replaces a
//! statement's comma-separated operands, drawing names from the scaffold's own registers.
use std::{borrow::Cow, num::NonZeroUsize, rc::Rc};
use libafl::{Error, mutators::{MutationResult, Mutator}, state::HasRand};
use libafl_bolts::{Named, rands::Rand, tuples::tuple_list};
use crate::{State, input::PtxInput, tokens::Vocabulary};

fn below(state: &mut State, n: usize) -> usize {
    NonZeroUsize::new(n).map_or(0, |n| state.rand_mut().below(n))
}

pub struct TokenSwap(Rc<Vocabulary>);
impl Named for TokenSwap {
    fn name(&self) -> &Cow<'static, str> { static NAME: Cow<'static, str> = Cow::Borrowed("token_swap"); &NAME }
}
impl Mutator<PtxInput, State> for TokenSwap {
    fn mutate(&mut self, state: &mut State, input: &mut PtxInput) -> Result<MutationResult, Error> {
        let candidates = self.0.candidates(&input.region);
        if candidates.is_empty() { return Ok(MutationResult::Skipped); }
        input.region = candidates[below(state, candidates.len())].1.clone();
        Ok(MutationResult::Mutated)
    }
    fn post_exec(&mut self, _: &mut State, _: Option<libafl::corpus::CorpusId>) -> Result<(), Error> { Ok(()) }
}

/// Operand names: the `%`-names the scaffold uses, plus two literals.
fn pool(input: &PtxInput) -> Vec<Vec<u8>> {
    let scaffold = input.scaffold();
    let mut names: Vec<Vec<u8>> = scaffold.split(|b| !(b.is_ascii_alphanumeric() || *b == b'%' || *b == b'_'))
        .filter(|w| w.len() > 1 && w[0] == b'%' && w.last().is_some_and(|c| c.is_ascii_digit())).map(<[u8]>::to_vec).collect();
    names.sort(); names.dedup();
    names.extend([b"0".to_vec(), b"1".to_vec()]);
    names
}

/// Byte range of a statement's operand text: after the head token (and any guard), before `;`.
fn operand_spans(region: &[u8]) -> Vec<(usize, usize)> {
    let mut spans = Vec::new(); let mut start = 0;
    for (i, &b) in region.iter().enumerate() {
        if b != b';' { continue; }
        let statement = &region[start..i];
        let skip = |s: &[u8], n: usize| s.iter().skip(n).position(|c| !c.is_ascii_whitespace()).map_or(s.len(), |p| p + n);
        let mut at = skip(statement, 0);
        if statement.get(at) == Some(&b'@') { at += statement[at..].iter().position(|c| c.is_ascii_whitespace()).unwrap_or(0); at = skip(statement, at); }
        let head_end = statement[at..].iter().position(|c| c.is_ascii_whitespace()).map_or(statement.len(), |p| at + p);
        if head_end > at { spans.push((start + head_end, i)); }
        start = i + 1;
    }
    spans
}

/// Top-level comma split of `text` (not inside `[]`, `{}` or `()`); returns operand byte ranges, trimmed.
fn operands(text: &[u8]) -> Vec<(usize, usize)> {
    let (mut depth, mut start, mut out) = (0i32, 0, Vec::new());
    for (i, &b) in text.iter().chain(b",").enumerate() {
        match b { b'[' | b'{' | b'(' => depth += 1, b']' | b'}' | b')' => depth -= 1, _ => {} }
        if (b == b',' && depth <= 0) || i == text.len() {
            let piece = &text[start..i.min(text.len())];
            let lead = piece.iter().take_while(|c| c.is_ascii_whitespace()).count();
            let trail = piece.iter().rev().take_while(|c| c.is_ascii_whitespace()).count();
            if lead + trail < piece.len() { out.push((start + lead, start + piece.len() - trail)); }
            start = i + 1;
        }
    }
    out
}

pub struct OperandEdit;
impl Named for OperandEdit {
    fn name(&self) -> &Cow<'static, str> { static NAME: Cow<'static, str> = Cow::Borrowed("operand_edit"); &NAME }
}
impl Mutator<PtxInput, State> for OperandEdit {
    fn mutate(&mut self, state: &mut State, input: &mut PtxInput) -> Result<MutationResult, Error> {
        let spans = operand_spans(&input.region);
        if spans.is_empty() { return Ok(MutationResult::Skipped); }
        let (from, to) = spans[below(state, spans.len())];
        let text = input.region[from..to].to_vec();
        let ops: Vec<Vec<u8>> = operands(&text).into_iter().map(|(a, b)| text[a..b].to_vec()).collect();
        let names = pool(input);
        let mut fresh = names[below(state, names.len())].clone();
        match below(state, 6) { // occasional address and vector forms
            0 => fresh = [b"[", &fresh[..], b"]"].concat(),
            1 => fresh = [b"{", &fresh[..], b"}"].concat(),
            _ => {}
        }
        let mut ops = ops;
        let at = below(state, ops.len() + 1);
        match below(state, 4) {
            0 => ops.insert(at, fresh),
            1 if !ops.is_empty() => { ops.remove(at.min(ops.len() - 1)); }
            2 if !ops.is_empty() => { let i = at.min(ops.len() - 1); ops.insert(i, ops[i].clone()); }
            _ if !ops.is_empty() => { let i = at.min(ops.len() - 1); ops[i] = fresh; }
            _ => ops.push(fresh),
        }
        let joined = [b" ".as_slice(), &ops.join(&b", "[..])].concat();
        input.region.splice(from..to, joined);
        Ok(MutationResult::Mutated)
    }
    fn post_exec(&mut self, _: &mut State, _: Option<libafl::corpus::CorpusId>) -> Result<(), Error> { Ok(()) }
}

pub fn mutations(vocabulary: Rc<Vocabulary>) -> (TokenSwap, (OperandEdit, ())) {
    tuple_list!(TokenSwap(vocabulary), OperandEdit)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn spans_and_operands() {
        let region = b" @%p1 add.u32 %r2, [%rd0], {%r1, %r3}; ret; ";
        let spans = operand_spans(region);
        assert_eq!(spans.len(), 2);
        let (a, b) = spans[0];
        let text = &region[a..b];
        let ops: Vec<_> = operands(text).into_iter().map(|(x, y)| &text[x..y]).collect();
        assert_eq!(ops, vec![&b"%r2"[..], b"[%rd0]", b"{%r1, %r3}"]);
        assert!(operands(&region[spans[1].0..spans[1].1]).is_empty());
    }
}
