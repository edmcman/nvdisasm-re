//! Virtual strcmp sites against the complete pinned opcode registry, independent
//! of ptxas's selected hash bucket. This trial uses equal-length root comparisons.
use std::{ops::Range, sync::LazyLock};
use libafl::observers::cmp::{AflppCmpLogHeader, AflppCmpValuesMetadata, CmpValues, CmplogBytes};
use crate::redqueen::MAP_WIDTH;

static ROOTS: LazyLock<Vec<Vec<u8>>> = LazyLock::new(|| {
    let registry: serde_json::Value = serde_json::from_str(include_str!("../../opcode_registry.json")).unwrap();
    registry["roots"].as_array().unwrap().iter().map(|name| name.as_str().unwrap().as_bytes().to_vec()).collect()
});

fn skip(data: &[u8], mut at: usize) -> usize {
    loop {
        while at < data.len() && (data[at].is_ascii_whitespace() || data[at] == 0x1a) { at += 1; }
        if data[at..].starts_with(b"//") {
            at += data[at..].iter().position(|&b| b == b'\n').unwrap_or(data.len() - at);
        } else if data[at..].starts_with(b"/*") {
            at += 2;
            at += data[at..].windows(2).position(|w| w == b"*/").map_or(data.len() - at, |n| n + 2);
        } else { return at; }
    }
}

fn name_byte(byte: u8) -> bool { byte.is_ascii_alphanumeric() || b"_$%".contains(&byte) }

/// Identify statement heads, excluding predicates, label definitions, comments,
/// strings and directives. Only names in the extracted registry are compared.
pub fn spans(data: &[u8]) -> Vec<Range<usize>> {
    let mut result = Vec::new();
    let (mut at, mut head) = (0, true);
    while at < data.len() {
        at = skip(data, at);
        if at == data.len() { break; }
        match data[at] {
            b';' => { head = true; at += 1; }
            b'{' | b'}' if head => { at += 1; }
            b'@' if head => {
                at += 1;
                if data.get(at) == Some(&b'!') { at += 1; }
                while at < data.len() && name_byte(data[at]) { at += 1; }
            }
            b'"' | b'\'' => {
                let quote = data[at]; at += 1;
                while at < data.len() {
                    if data[at] == b'\\' { at = (at + 2).min(data.len()); }
                    else if data[at] == quote { at += 1; break; }
                    else { at += 1; }
                }
                head = false;
            }
            byte if name_byte(byte) && !byte.is_ascii_digit() => {
                let start = at;
                while at < data.len() && name_byte(data[at]) { at += 1; }
                if head {
                    let next = skip(data, at);
                    if data.get(next) == Some(&b':') { at = next + 1; }
                    else {
                        if ROOTS.iter().any(|name| name == &data[start..at]) { result.push(start..at); }
                        head = false;
                    }
                }
            }
            _ => { if head { head = false; } at += 1; }
        }
    }
    result
}

fn bytes(value: &[u8]) -> CmplogBytes {
    let mut buffer = [0; 32]; buffer[..value.len()].copy_from_slice(value);
    CmplogBytes::from_buf_and_len(buffer, value.len() as u8)
}

pub fn comparisons(data: &[u8]) -> AflppCmpValuesMetadata {
    let mut metadata = AflppCmpValuesMetadata::new();
    for span in spans(data) {
        let query = &data[span];
        if query.len() > 32 { continue; }
        for (ordinal, target) in ROOTS.iter().enumerate() {
            if target.len() != query.len() || target == query { continue; }
            // Virtual sites live outside QEMU's map and cannot overwrite real
            // comparison sites. Each registry root has one stable site ordinal.
            let site = MAP_WIDTH + ordinal;
            if !metadata.orig_cmpvals.contains_key(&site) {
                let header = 1 | (((query.len() - 1) as u16) << 6) | (1 << 11);
                metadata.headers.push((site, AflppCmpLogHeader::new_with_raw_value(header)));
            }
            let value = CmpValues::Bytes((bytes(query), bytes(target)));
            metadata.orig_cmpvals.entry(site).or_default().push(value.clone());
            // A virtual string comparison is known to consume exactly this
            // token. The string path in pinned RedQueen also handles unchanged
            // colorization bytes; no synthetic compiler execution is implied.
            metadata.new_cmpvals.entry(site).or_default().push(value);
        }
    }
    metadata
}

/// RedQueen emits partial matches and can match repeated words in operands.
/// Retain complete registry roots and edits confined to opcode-token spans.
pub fn valid_replacement(original: &[u8], candidate: &[u8], spans: &[Range<usize>]) -> bool {
    original.len() == candidate.len() && original != candidate
        && original.iter().zip(candidate).enumerate().all(|(i, (a, b))| a == b || spans.iter().any(|s| s.contains(&i)))
        && spans.iter().all(|s| ROOTS.iter().any(|name| name == &candidate[s.clone()]))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn statement_heads_only() {
        let input = b"/* add; */ sub: @!%p0 add.u32 %r2, %r0, %r1; // xor;\n .reg .u32 add; mov.u32 %r2, add;\n bra sub;";
        assert_eq!(spans(input).iter().map(|s| &input[s.clone()]).collect::<Vec<_>>(), vec![b"add".as_slice(), b"mov", b"bra"]);
        assert!(spans(b"// add").is_empty());
        assert!(spans(b"/* unclosed add;").is_empty());
        assert!(spans(b"\"add; xor;\"").is_empty());
    }

    #[test]
    fn repeated_queries_share_stable_sites() {
        let one = comparisons(b"add.u32 %r2, %r0, %r1;");
        let two = comparisons(b"add.u32 %r2, %r0, %r1; add.u32 %r3, %r0, %r1;");
        assert_eq!(one.headers.iter().map(|(site, _)| site).collect::<Vec<_>>(), two.headers.iter().map(|(site, _)| site).collect::<Vec<_>>());
        assert!(!one.headers.is_empty());
        assert!(one.headers.iter().all(|(site, _)| *site >= MAP_WIDTH));
        assert!(two.orig_cmpvals.values().all(|values| values.len() == 2));
    }
}
