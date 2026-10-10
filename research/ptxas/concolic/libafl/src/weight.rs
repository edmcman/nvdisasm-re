//! Scheduling weight that favours entries holding rare semantic slots. Map indexes from `EDGES`
//! on (parser recognition, PTX/SASS facts) are hashes of what the compiler accepted or
//! recognised, so unlike qemu edge ids they mean the same thing in every entry.
use std::{cell::RefCell, collections::HashMap};
use libafl::{Error, HasMetadata, corpus::{Corpus, Testcase}, feedbacks::{MapIndexesMetadata, MapNoveltiesMetadata},
             schedulers::{TestcaseScore, testcase_score::CorpusWeightTestcaseScore}, state::HasCorpus};
use crate::{State, features::EDGES, input::PtxInput};

/// Weight multiplier for an entry that alone holds a semantic slot and has not been scheduled.
const BOOST: f64 = 50.0;

thread_local! { static HOLDERS: RefCell<(usize, HashMap<usize, u32>)> = RefCell::new((usize::MAX, HashMap::new())); }

fn semantic(indexes: &MapIndexesMetadata) -> &[usize] {
    &indexes.list[indexes.list.partition_point(|&i| i < EDGES)..]
}

/// `CorpusWeightTestcaseScore` times `1 + BOOST * sum(1 / holders(slot)) / (1 + times scheduled)`
/// times `1 + log2(1 + qemu edges new at admission) / (1 + times scheduled)`.
pub struct RarityScore;
impl TestcaseScore<PtxInput, State> for RarityScore {
    fn compute(state: &State, entry: &mut Testcase<PtxInput>) -> Result<f64, Error> {
        let base = CorpusWeightTestcaseScore::compute(state, entry)?;
        let Some(own) = entry.metadata_map().get::<MapIndexesMetadata>() else { return Ok(base) };
        // The caller holds `entry` mutably, so the table is built from every other entry plus `entry`
        // itself; it is rebuilt only when the corpus size changes.
        let rarity = HOLDERS.with(|cache| {
            let mut cache = cache.borrow_mut();
            if cache.0 != state.corpus().count() {
                let mut holders = HashMap::new();
                for id in state.corpus().ids() {
                    let Ok(other) = state.corpus().get(id).unwrap().try_borrow() else { continue };
                    if let Some(indexes) = other.metadata_map().get::<MapIndexesMetadata>() {
                        for &slot in semantic(indexes) { *holders.entry(slot).or_insert(0u32) += 1; }
                    }
                }
                for &slot in semantic(own) { *holders.entry(slot).or_insert(0u32) += 1; }
                *cache = (state.corpus().count(), holders);
            }
            semantic(own).iter().map(|slot| 1.0 / f64::from(cache.1.get(slot).copied().unwrap_or(1).max(1))).sum::<f64>()
        });
        let fresh = 1.0 / (1.0 + entry.scheduled_count() as f64);
        let new_edges = entry.metadata_map().get::<MapNoveltiesMetadata>().map_or(0, |n| n.list.iter().filter(|&&i| i < EDGES).count());
        Ok(base * (1.0 + BOOST * rarity * fresh) * (1.0 + (1.0 + new_edges as f64).log2() * fresh))
    }
}
