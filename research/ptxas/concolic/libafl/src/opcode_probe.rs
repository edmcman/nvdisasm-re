//! One RedQueen pass per arm, with no dictionary or havoc, compiled by real ptxas.
use std::{fs, path::Path, process::Command, time::Duration};
use libafl::{HasMetadata, corpus::{Corpus, HasCurrentCorpusId, InMemoryCorpus, InMemoryOnDiskCorpus, Testcase},
    events::SimpleEventManager, executors::{StdChildArgs, forkserver::ForkserverExecutor},
    feedbacks::ConstFeedback, fuzzer::StdFuzzer, monitors::SimpleMonitor,
    mutators::MultiMutator, observers::{HitcountsMapObserver, StdMapObserver},
    schedulers::QueueScheduler, stages::{Stage, colorization::ColorizationStage},
    state::HasCorpus};
use libafl_bolts::{StdTargetArgs, rands::StdRand,
    shmem::{ShMem, ShMemProvider, UnixShMemProvider}, tuples::{Handled, tuple_list}};
use libafl_targets::cmps::{AflppCmpLogMap, observers::AflppCmpLogObserver};
use crate::{State, input::PtxInput, redqueen};

pub fn run(seed: &Path, root: &Path) {
    assert!(!root.exists(), "Probe output already exists");
    fs::create_dir_all(root).unwrap();
    let root = root.canonicalize().unwrap();
    let compiler = Path::new("/usr/local/cuda-13.0/bin/ptxas");
    let qemu = Path::new(env!("CARGO_MANIFEST_DIR")).join("../tools/AFLplusplus/afl-qemu-trace").canonicalize().unwrap();
    let input = PtxInput::parse(&fs::read(seed).unwrap()).expect("Instruction markers required");
    fs::write(root.join("seed.ptx"), input.bytes()).unwrap();
    assert!(Command::new(compiler).args(["-arch=sm_75", "-o", "/dev/null"]).arg(root.join("seed.ptx")).status().unwrap().success(), "Seed must compile for SM75");
    let hash = Command::new("sha256sum").arg(compiler).output().unwrap();
    let compiler_hash = String::from_utf8(hash.stdout).unwrap().split_whitespace().next().unwrap().to_owned();
    let registry: serde_json::Value = serde_json::from_str(include_str!("../../opcode_registry.json")).unwrap();
    assert_eq!(compiler_hash, registry["compiler_sha256"].as_str().unwrap(), "Probe requires pinned ptxas");
    let mut state = State::new(StdRand::with_seed(1), InMemoryOnDiskCorpus::no_meta(root.join("queue")).unwrap(),
        InMemoryCorpus::new(), &mut ConstFeedback::new(false), &mut ConstFeedback::new(false)).unwrap();
    let id = state.corpus_mut().add(Testcase::new(input.clone())).unwrap();
    state.set_corpus_id(id).unwrap();
    let mut sp = UnixShMemProvider::new().unwrap();
    let mut edges_mem = sp.new_shmem(crate::features::EDGES).unwrap();
    let edges_id = edges_mem.id().to_string();
    unsafe { edges_mem.write_to_env("__AFL_SHM_ID").unwrap(); }
    let edges = unsafe { HitcountsMapObserver::new(StdMapObserver::new("edges", &mut edges_mem[..])) };
    let mut colorization = ColorizationStage::new(&edges);
    let mut executor = ForkserverExecutor::builder().program(&qemu).arg(compiler)
        .env("__AFL_SHM_ID", &edges_id).env("AFL_QEMU_MAP_SIZE", crate::features::EDGES.to_string())
        .env("AFL_ENTRYPOINT", "0x4428e0").coverage_map_size(crate::features::EDGES)
        .shmem_provider(&mut sp).timeout(Duration::from_secs(2))
        .arg("-arch=sm_75").arg("-o").arg(root.join("normal.cubin"))
        .arg_input_file(root.join("normal.input")).build(tuple_list!(edges)).unwrap();
    let mut cmp_mem = sp.new_shmem(redqueen::MAP_SIZE).unwrap(); cmp_mem.fill(0);
    let cmp_id = cmp_mem.id().to_string();
    let observer = AflppCmpLogObserver::new("cmplog", unsafe { AflppCmpLogMap::from_shmem(&mut cmp_mem) }, true);
    let handle = observer.handle();
    let mut tracer = ForkserverExecutor::builder().program(&qemu).arg(compiler)
        .env("__AFL_SHM_ID", edges_id).env("__AFL_CMPLOG_SHM_ID", cmp_id)
        .env("___AFL_EINS_ZWEI_POLIZEI___", "1").env("AFL_QEMU_MAP_SIZE", crate::features::EDGES.to_string())
        .env("AFL_ENTRYPOINT", "0x4428e0").coverage_map_size(crate::features::EDGES)
        .shmem_provider(&mut sp).timeout(Duration::from_secs(2))
        .arg("-arch=sm_75").arg("-o").arg(root.join("tracer.cubin"))
        .arg_input_file(root.join("tracer.input")).build(tuple_list!(observer)).unwrap();
    let mut fuzzer = StdFuzzer::new(QueueScheduler::new(), ConstFeedback::new(false), ConstFeedback::new(false));
    let mut manager = SimpleEventManager::new(SimpleMonitor::new(|_| {}));
    colorization.perform(&mut fuzzer, &mut executor, &mut state, &mut manager).unwrap();
    redqueen::trace(&mut tracer, &handle, &mut fuzzer, &mut state, &mut manager).unwrap();
    let baseline = redqueen::RegionRedQueen::new().multi_mutate(&mut state, &input, None).unwrap();
    let synthetic = redqueen::OpcodeRedQueen::new().multi_mutate(&mut state, &input, None).unwrap();
    let mut arms = serde_json::Map::new();
    for (name, candidates) in [("baseline", baseline), ("synthetic", synthetic)] {
        let directory = root.join(name); fs::create_dir(&directory).unwrap();
        let mut witnesses = Vec::new();
        for (i, candidate) in candidates.iter().enumerate() {
            let path = directory.join(format!("{i:04}.ptx")); fs::write(&path, candidate.bytes()).unwrap();
            let cubin = path.with_extension("cubin");
            let result = Command::new(compiler).arg("-arch=sm_75").arg("-o").arg(&cubin).arg(&path).output().unwrap();
            let roots: Vec<_> = crate::opcode_cmp::spans(&candidate.region).iter()
                .map(|s| String::from_utf8_lossy(&candidate.region[s.clone()]).into_owned()).collect();
            witnesses.push(serde_json::json!({"source":path,"accepted":result.status.success(),"opcode_roots":roots,
                "region":String::from_utf8_lossy(&candidate.region),"diagnostic":String::from_utf8_lossy(&result.stderr)}));
        }
        let mut accepted_roots: Vec<_> = witnesses.iter().filter(|w| w["accepted"] == true)
            .flat_map(|w| w["opcode_roots"].as_array().unwrap().iter().cloned()).collect();
        accepted_roots.sort_by(|a,b| a.as_str().cmp(&b.as_str())); accepted_roots.dedup();
        arms.insert(name.into(), serde_json::json!({"candidates":candidates.len(),
            "accepted":witnesses.iter().filter(|w| w["accepted"] == true).count(),
            "accepted_opcode_roots":accepted_roots,"witnesses":witnesses}));
    }
    let summary = serde_json::json!({"compiler_sha256":compiler_hash,"architecture":"SM75",
        "dictionary":false,"havoc":false,"baseline_stats":state.metadata::<redqueen::Stats>().unwrap(),
        "synthetic_stats":state.metadata::<redqueen::OpcodeStats>().unwrap(),"arms":arms});
    fs::write(root.join("summary.json"), serde_json::to_vec_pretty(&summary).unwrap()).unwrap();
    for name in ["baseline", "synthetic"] { println!("{name}: {} candidates, {} accepted, roots {}",
        summary["arms"][name]["candidates"], summary["arms"][name]["accepted"], summary["arms"][name]["accepted_opcode_roots"]); }
}
