//! Region-only RedQueen mutations, with complete PTX files used for every execution.
use std::borrow::Cow;
use libafl::{Error, HasMetadata,
    corpus::{CorpusId, HasCurrentCorpusId, InMemoryCorpus},
    executors::{Executor, HasObservers}, inputs::BytesInput,
    mutators::{MultiMutator, AflppRedQueen},
    observers::{ObserversTuple, cmp::AflppCmpValuesMetadata},
    stages::colorization::TaintMetadata,
    state::{HasCorpus, HasCurrentTestcase, HasMaxSize, HasRand}};
use libafl_bolts::{Named, rands::StdRand, serdeany::SerdeAnyMap, tuples::Handle, tuples::RefIndexable};
use libafl_targets::cmps::{AflppCmpLogMap, observers::AflppCmpLogObserver};
use crate::{State, input::PtxInput};

// QEMU's pinned cmp_map has the LibAFL map as its prefix, followed by these arrays.
// Allocate the whole map: QEMU uses the trailing fields to identify comparison sites.
pub const MAP_WIDTH: usize = 65536;
pub const MAP_SIZE: usize = std::mem::size_of::<AflppCmpLogMap>() + MAP_WIDTH * (4 + 2);

#[derive(Debug, Default, Clone, serde::Serialize, serde::Deserialize)]
pub struct Stats {
    pub attempts: u64,
    pub comparison_sites: u64,
    pub candidates: u64,
    pub admitted: u64,
}
libafl_bolts::impl_serdeany!(Stats);

pub fn stats(state: &mut State) -> &mut Stats { state.metadata_or_insert_with(Stats::default) }

/// Stop between executions as well as during forkserver waits. Colorization and
/// RedQueen can run many inputs inside one fuzz_one call.
pub struct Stoppable<E>(pub E);
impl<E: HasObservers> HasObservers for Stoppable<E> {
    type Observers = E::Observers;
    fn observers(&self) -> RefIndexable<&Self::Observers, Self::Observers> { self.0.observers() }
    fn observers_mut(&mut self) -> RefIndexable<&mut Self::Observers, Self::Observers> { self.0.observers_mut() }
}
impl<E, EM, I, S, Z> Executor<EM, I, S, Z> for Stoppable<E>
where E: Executor<EM, I, S, Z> {
    fn run_target(&mut self, fuzzer: &mut Z, state: &mut S, manager: &mut EM, input: &I) -> Result<libafl::executors::ExitKind, Error> {
        if crate::STOP.load(std::sync::atomic::Ordering::Relaxed) { return Err(Error::unknown("worker stopping")); }
        self.0.run_target(fuzzer, state, manager, input)
    }
}

/// AflppRedQueen only accesses metadata and the current ID, but its pinned signature
/// also requires HasCorpus<BytesInput>. Keep that unused corpus separate from PTX state.
struct ByteState<'a> { state: &'a mut State, corpus: InMemoryCorpus<BytesInput> }
impl HasMetadata for ByteState<'_> {
    fn metadata_map(&self) -> &SerdeAnyMap { self.state.metadata_map() }
    fn metadata_map_mut(&mut self) -> &mut SerdeAnyMap { self.state.metadata_map_mut() }
}
impl HasRand for ByteState<'_> {
    type Rand = StdRand;
    fn rand(&self) -> &StdRand { self.state.rand() }
    fn rand_mut(&mut self) -> &mut StdRand { self.state.rand_mut() }
}
impl HasMaxSize for ByteState<'_> {
    fn max_size(&self) -> usize { self.state.max_size() }
    fn set_max_size(&mut self, size: usize) { self.state.set_max_size(size); }
}
impl HasCorpus<BytesInput> for ByteState<'_> {
    type Corpus = InMemoryCorpus<BytesInput>;
    fn corpus(&self) -> &Self::Corpus { &self.corpus }
    fn corpus_mut(&mut self) -> &mut Self::Corpus { &mut self.corpus }
}
impl HasCurrentCorpusId for ByteState<'_> {
    fn current_corpus_id(&self) -> Result<Option<CorpusId>, Error> { self.state.current_corpus_id() }
    fn set_corpus_id(&mut self, id: CorpusId) -> Result<(), Error> { self.state.set_corpus_id(id) }
    fn clear_corpus_id(&mut self) -> Result<(), Error> { self.state.clear_corpus_id() }
}

pub struct RegionRedQueen(AflppRedQueen);
impl RegionRedQueen { pub fn new() -> Self { Self(AflppRedQueen::with_cmplog_options(true, true)) } }
impl Named for RegionRedQueen {
    fn name(&self) -> &Cow<'static, str> { self.0.name() }
}
impl MultiMutator<PtxInput, State> for RegionRedQueen {
    fn multi_mutate(&mut self, state: &mut State, input: &PtxInput, max: Option<usize>) -> Result<Vec<PtxInput>, Error> {
        if crate::STOP.load(std::sync::atomic::Ordering::Relaxed) { return Ok(Vec::new()); }
        let mut view = ByteState { state, corpus: InMemoryCorpus::new() };
        let generated = self.0.multi_mutate(&mut view, &BytesInput::new(input.region.clone()), max)?;
        stats(view.state).candidates += generated.len() as u64;
        Ok(generated.into_iter().map(|bytes| input.with_region(bytes.into())).collect())
    }
    fn multi_post_exec(&mut self, state: &mut State, id: Option<CorpusId>) -> Result<(), Error> {
        if id.is_some() { stats(state).admitted += 1; }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use libafl::{corpus::{Corpus, InMemoryOnDiskCorpus, Testcase}, feedbacks::ConstFeedback,
        mutators::Tokens,
        observers::cmp::{AflppCmpLogHeader, CmpValues, CmplogBytes}};
    use libafl_bolts::{shmem::{ShMemProvider, UnixShMemProvider}, HasLen};

    fn state(path: &std::path::Path, seed: &PtxInput) -> State {
        let mut state = State::new(StdRand::with_seed(1), InMemoryOnDiskCorpus::no_meta(path).unwrap(),
            InMemoryCorpus::new(), &mut ConstFeedback::new(false), &mut ConstFeedback::new(false)).unwrap();
        let id = state.corpus_mut().add(Testcase::new(seed.clone())).unwrap();
        state.set_corpus_id(id).unwrap();
        state
    }

    #[test]
    fn replacements_keep_scaffold_and_harvest_tokens() {
        for (index, scaffold) in [
            b"head\n// BEGIN_INSTRUCTION\naaaa\n// END_INSTRUCTION\ntail\n".as_slice(),
            b"different /* BEGIN_INSTRUCTION */aaaa/* END_INSTRUCTION */ suffix".as_slice(),
        ].into_iter().enumerate() {
            let seed = PtxInput::parse(scaffold).unwrap();
            let root = std::env::temp_dir().join(format!("ptx-rq-unit-{}-{index}", std::process::id()));
            let mut state = state(&root, &seed);
            // A checkpoint predating RedQueen has no Stats or comparison metadata.
            state = postcard::from_bytes(&postcard::to_allocvec(&state).unwrap()).unwrap();
            assert!(state.metadata_map().get::<Stats>().is_none());
            for string in [false, true] {
                let mut colored = seed.region.clone(); colored[..4].copy_from_slice(b"bbbb");
                state.add_metadata(TaintMetadata::new(colored, vec![0..4]));
                let mut meta = AflppCmpValuesMetadata::new();
                let header = 1 | (3 << 6) | (u16::from(string) << 11);
                meta.headers.push((0, AflppCmpLogHeader::new_with_raw_value(header)));
                let values = |bytes: &[u8; 4]| if string {
                    let mut buf = [0; 32]; buf[..4].copy_from_slice(bytes);
                    let mut target = [0; 32]; target[..4].copy_from_slice(b"PASS");
                    CmpValues::Bytes((CmplogBytes::from_buf_and_len(buf, 4), CmplogBytes::from_buf_and_len(target, 4)))
                } else { CmpValues::U32((u32::from_le_bytes(*bytes), u32::from_le_bytes(*b"PASS"), false)) };
                meta.orig_cmpvals.insert(0, vec![values(b"aaaa")]);
                meta.new_cmpvals.insert(0, vec![values(b"bbbb")]);
                state.add_metadata(meta);
                let candidates = RegionRedQueen::new().multi_mutate(&mut state, &seed, None).unwrap();
                assert!(candidates.iter().any(|input| input.region.starts_with(b"PASS")), "string={string}");
                let (begin, end) = seed.span();
                for candidate in candidates {
                    let bytes = candidate.bytes();
                    assert_eq!(&bytes[..begin], &scaffold[..begin]);
                    assert_eq!(&bytes[begin + candidate.region.len()..], &scaffold[end..]);
                }
            }
            assert!(state.metadata::<Tokens>().unwrap().tokens().iter().any(|token| token == b"PASS"));
            // New stats metadata must survive the same checkpoint codec used by workers.
            let restored: State = postcard::from_bytes(&postcard::to_allocvec(&state).unwrap()).unwrap();
            assert!(restored.metadata::<Stats>().unwrap().candidates > 0);
            drop(restored); drop(state); std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    #[ignore = "requires pinned AFL++ headers and a C compiler"]
    fn qemu_map_layout() {
        use std::process::Command;
        use libafl_targets::cmps::{AflppCmpLogOperands, AflppCmpLogFnOperands};
        let header = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../tools/AFLplusplus/include");
        let root = std::env::temp_dir().join(format!("ptx-rq-layout-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let source = root.join("layout.c"); let binary = root.join("layout");
        std::fs::write(&source, r#"
#include <stdio.h>
#include <stddef.h>
#include "cmplog.h"
int main(void) {
  printf("%zu %zu %zu %zu %zu %zu %zu\n", sizeof(struct cmp_map),
    offsetof(struct cmp_map, site_ids), sizeof(struct cmp_header),
    sizeof(struct cmp_operands), sizeof(struct cmpfn_operands),
    offsetof(struct cmp_operands, v1), offsetof(struct cmpfn_operands, v1));
}
"#).unwrap();
        assert!(Command::new("cc").arg("-I").arg(header).arg(&source).arg("-o").arg(&binary).status().unwrap().success());
        let output = Command::new(&binary).output().unwrap(); assert!(output.status.success());
        let sizes: Vec<usize> = String::from_utf8(output.stdout).unwrap().split_whitespace().map(|x| x.parse().unwrap()).collect();
        assert_eq!(sizes, vec![MAP_SIZE, std::mem::size_of::<AflppCmpLogMap>(),
            std::mem::size_of::<AflppCmpLogHeader>(), std::mem::size_of::<AflppCmpLogOperands>(),
            std::mem::size_of::<AflppCmpLogFnOperands>(), 32, 32]);
        let mut sp = UnixShMemProvider::new().unwrap(); let mut memory = sp.new_shmem(MAP_SIZE).unwrap();
        memory.fill(0);
        let map = unsafe { AflppCmpLogMap::from_shmem(&mut memory) };
        assert_eq!(map.as_ref().len(), MAP_WIDTH);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    #[ignore = "requires pinned afl-qemu-trace and a C compiler"]
    fn qemu_solves_comparison_in_complete_kernel() {
        use std::{path::Path, process::Command, time::Duration};
        use libafl::{events::SimpleEventManager, executors::{forkserver::ForkserverExecutor, StdChildArgs},
            fuzzer::StdFuzzer, monitors::SimpleMonitor, observers::{HitcountsMapObserver, StdMapObserver},
            schedulers::QueueScheduler, stages::{Stage, colorization::ColorizationStage}};
        use libafl_bolts::{StdTargetArgs, shmem::ShMem, tuples::{Handled, tuple_list}};
        let qemu = Path::new(env!("CARGO_MANIFEST_DIR")).join("../tools/AFLplusplus/afl-qemu-trace").canonicalize().unwrap();
        let root = std::env::temp_dir().join(format!("ptx-rq-qemu-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let source = root.join("target.c"); let target = root.join("target");
        // The target rejects scaffold changes and compares only the four region bytes.
        std::fs::write(&source, r#"
#include <stdio.h>
#include <stdint.h>
#include <string.h>
int main(int argc, char **argv) {
  char input[4096] = {0};
  if (argc != 2) return 2;
  FILE *f = fopen(argv[1], "rb"); if (!f) return 2;
  size_t n = fread(input, 1, sizeof(input)-1, f); fclose(f);
  const char prefix[] = "head\n// BEGIN_INSTRUCTION\n";
  const char suffix[] = "\n// END_INSTRUCTION\ntail\n";
  size_t p = sizeof(prefix)-1, s = sizeof(suffix)-1;
  if (n != p+4+s || memcmp(input, prefix, p) || memcmp(input+p+4, suffix, s)) return 3;
  uint32_t value; memcpy(&value, input+p, 4);
  if (value == UINT32_C(0x32343234)) puts("solved");
  return 0;
}
"#).unwrap();
        assert!(Command::new("cc").arg("-O1").arg(&source).arg("-o").arg(&target).status().unwrap().success());
        let seed = PtxInput::parse(b"head\n// BEGIN_INSTRUCTION\n1111\n// END_INSTRUCTION\ntail\n").unwrap();
        // The fixture also checks the region's trailing newline; colorization must
        // retain it when changing it would take the scaffold-rejection branch.
        let mut state = state(&root.join("queue"), &seed);
        let mut sp = UnixShMemProvider::new().unwrap();
        let mut edges_mem = sp.new_shmem(65536).unwrap();
        let edges_id = edges_mem.id().to_string();
        unsafe { edges_mem.write_to_env("__AFL_SHM_ID").unwrap(); }
        let edges = unsafe { HitcountsMapObserver::new(StdMapObserver::new("edges", &mut edges_mem[..])) };
        let mut colorization = ColorizationStage::new(&edges);
        let mut executor = ForkserverExecutor::builder().program(&qemu).arg(&target)
            .env("__AFL_SHM_ID", &edges_id).coverage_map_size(65536)
            .shmem_provider(&mut sp).timeout(Duration::from_secs(2))
            .arg_input_file(root.join("normal.input")).build(tuple_list!(edges)).unwrap();
        let mut cmp_mem = sp.new_shmem(MAP_SIZE).unwrap(); cmp_mem.fill(0);
        let cmp_id = cmp_mem.id().to_string();
        let observer = AflppCmpLogObserver::new("cmplog", unsafe { AflppCmpLogMap::from_shmem(&mut cmp_mem) }, true);
        let handle = observer.handle();
        let mut tracer = ForkserverExecutor::builder().program(&qemu).arg(&target)
            .env("__AFL_SHM_ID", edges_id).env("__AFL_CMPLOG_SHM_ID", cmp_id)
            .env("___AFL_EINS_ZWEI_POLIZEI___", "1").coverage_map_size(65536)
            .shmem_provider(&mut sp).timeout(Duration::from_secs(2))
            .arg_input_file(root.join("tracer.input")).build(tuple_list!(observer)).unwrap();
        let mut fuzzer = StdFuzzer::new(QueueScheduler::new(), ConstFeedback::new(false), ConstFeedback::new(false));
        let mut manager = SimpleEventManager::new(SimpleMonitor::new(|_| {}));
        colorization.perform(&mut fuzzer, &mut executor, &mut state, &mut manager).unwrap();
        let colored = seed.with_region(state.metadata::<TaintMetadata>().unwrap().input_vec().clone());
        assert_eq!(Command::new(&target).arg({ let p = root.join("colored.input"); std::fs::write(&p, colored.bytes()).unwrap(); p }).status().unwrap().code(), Some(0));
        trace(&mut tracer, &handle, &mut fuzzer, &mut state, &mut manager).unwrap();
        assert!(state.metadata::<Stats>().unwrap().comparison_sites > 0);
        let candidates = RegionRedQueen::new().multi_mutate(&mut state, &seed, None).unwrap();
        let solved = candidates.iter().find(|candidate| candidate.region.starts_with(b"4242")).expect("RedQueen must solve the logged comparison");
        let path = root.join("solved.input"); std::fs::write(&path, solved.bytes()).unwrap();
        let output = Command::new(&target).arg(path).output().unwrap();
        assert!(output.status.success()); assert_eq!(output.stdout, b"solved\n");
        drop(tracer); drop(executor); drop(state);
        std::fs::remove_dir_all(root).unwrap();
    }
}

/// Trace two complete kernels. LibAFL's stock tracing stage constructs BytesInput
/// directly from taint bytes and would omit the PTX scaffold on the second run.
pub fn trace<TE, EM, Z>(tracer: &mut TE, handle: &Handle<AflppCmpLogObserver<'_>>,
                       fuzzer: &mut Z, state: &mut State, manager: &mut EM) -> Result<(), Error>
where TE: HasObservers + Executor<EM, PtxInput, State, Z>,
      TE::Observers: ObserversTuple<PtxInput, State> {
    let input = state.current_input_cloned()?;
    let region = state.metadata::<TaintMetadata>()?.input_vec().clone();
    for (original, kernel) in [(true, input.clone()), (false, input.with_region(region))] {
        if crate::STOP.load(std::sync::atomic::Ordering::Relaxed) { return Ok(()); }
        tracer.observers_mut()[handle].set_original(original);
        tracer.observers_mut().pre_exec_all(state, &kernel)?;
        let exit = tracer.run_target(fuzzer, state, manager, &kernel)?;
        tracer.observers_mut().post_exec_all(state, &kernel, &exit)?;
    }
    let sites = state.metadata::<AflppCmpValuesMetadata>()?.headers().len();
    stats(state).comparison_sites += sites as u64;
    Ok(())
}
