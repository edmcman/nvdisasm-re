//! PTX and SASS facts about a run, written as extra coverage-map slots past qemu's edges, so
//! LibAFL's feedback, favored-entry selection and power schedule treat a new opcode, modifier,
//! operand shape or SASS opcode like a new edge.
use std::hash::{DefaultHasher, Hash, Hasher};

/// qemu edge slots (AFL_QEMU_MAP_SIZE); feature slots follow.
pub const EDGES: usize = 1 << 20;
pub const SLOTS: usize = 1 << 16;

fn slot(fact: impl Hash) -> usize {
    let mut h = DefaultHasher::new();
    fact.hash(&mut h);
    EDGES + (h.finish() as usize) % SLOTS
}

fn operand_kind(operand: &str) -> char {
    match operand.trim().chars().next() {
        Some('%') => 'r',
        Some('[') => 'm',
        Some('{') => 'v',
        Some('!') => 'n',
        Some(c) if c.is_ascii_digit() || c == '-' || c == '+' => 'i',
        Some(c) if c.is_alphabetic() || c == '_' || c == '$' => 'l',
        _ => '?',
    }
}

/// Mnemonic, full opcode, each modifier and the operand-kind shape of every statement in the
/// instruction region of a file ptxas accepted. Rejected files get no facts, so misspellings
/// (`lmov`) are not rewarded.
pub fn ptx(region: &[u8]) -> Vec<usize> {
    let text = String::from_utf8_lossy(region);
    let mut slots = vec![];
    for statement in text.split(';') {
        // Leading label definitions and a guard are not the opcode; labels alone carry no facts.
        let mut words = statement.split(|c: char| c.is_whitespace() || c == '\x1a').filter(|w| !w.is_empty())
            .skip_while(|w| w.ends_with(':') || w.starts_with('@')).peekable();
        let Some(opcode) = words.next().filter(|w| w.starts_with(|c: char| c.is_ascii_alphabetic()) && !w.contains(':'))
            else { continue };
        let operands: String = words.collect::<Vec<_>>().join(" ");
        let mut parts = opcode.split('.');
        let base = parts.next().unwrap_or_default();
        let shape: String = operands.split(',').filter(|o| !o.trim().is_empty()).map(operand_kind).collect();
        slots.extend([slot(("base", base)), slot(("opcode", opcode)), slot(("shape", base, &shape))]);
        slots.extend(parts.map(|m| slot(("modifier", base, m))));
    }
    slots
}

/// SASS opcode (md field BITS_13_91_91_11_0: bit 91, bits 11..0) of every instruction in the
/// cubin's executable sections.
pub fn sass(cubin: &[u8]) -> Vec<usize> {
    let u16_at = |o: usize| cubin.get(o..o + 2).map(|b| u16::from_le_bytes(b.try_into().unwrap()) as usize);
    let u64_at = |o: usize| cubin.get(o..o + 8).map(|b| u64::from_le_bytes(b.try_into().unwrap()));
    let (Some(table), Some(size), Some(count)) = (u64_at(0x28), u16_at(0x3a), u16_at(0x3c)) else { return vec![] };
    (0..count).filter_map(|i| {
        let header = table as usize + i * size;
        let (flags, offset, length) = (u64_at(header + 8)?, u64_at(header + 0x18)? as usize, u64_at(header + 0x20)? as usize);
        (flags & 4 != 0).then(|| cubin.get(offset..offset + length)).flatten()
    }).flat_map(|text| text.chunks_exact(16).map(|w| {
        let (lo, hi) = (u64::from_le_bytes(w[..8].try_into().unwrap()), u64::from_le_bytes(w[8..].try_into().unwrap()));
        slot(("sass", (lo & 0xfff) | ((hi >> 27) & 1) << 12))
    }).collect::<Vec<_>>()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn ptx_facts() {
        let add = ptx(b" add.u32 %r2, %r0, %r1; ");
        assert_eq!(add.len(), 3 + 1);
        assert_ne!(add, ptx(b" sub.u32 %r2, %r0, %r1; "));
        assert_ne!(add, ptx(b" add.u32 %r2, %r0, 7; "));
        assert_eq!(ptx(b" @!%p0 add.u32 %r2, %r0, %r1; "), add);
        assert_eq!(ptx(b" L1: \x1aadd.u32 %r2, %r0, %r1; "), add);
        assert!(ptx(b" Haddprdt: ").is_empty() && ptx(b" st:atoYad:toYad: ").is_empty());
        assert!(add.iter().all(|&s| (EDGES..EDGES + SLOTS).contains(&s)));
    }

    #[test]
    fn sass_facts() {
        let (ptxas, dir) = ("/usr/local/cuda-13.0/bin/ptxas", std::env::temp_dir().join("ptx-sass-gen-test.cubin"));
        if !std::path::Path::new(ptxas).exists() { return; }
        let seed = concat!(env!("CARGO_MANIFEST_DIR"), "/../generic_sm75.ptx");
        assert!(std::process::Command::new(ptxas).args(["-arch=sm_75", "-o"]).arg(&dir).arg(seed).status().unwrap().success());
        let slots = sass(&std::fs::read(&dir).unwrap());
        // LDC/ULDC/IADD3/STG/EXIT/BRA/NOP at least; padding NOPs repeat one opcode.
        assert!(slots.len() >= 8 && slots.iter().collect::<HashSet<_>>().len() >= 5, "{slots:?}");
    }
}
