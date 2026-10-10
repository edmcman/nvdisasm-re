//! Verify recognition feedback excludes arbitrary spellings and retains known invalid forms.
use std::{fs, path::Path, process::Command, time::Duration};
use libafl::{corpus::{InMemoryCorpus, InMemoryOnDiskCorpus}, events::SimpleEventManager,
    executors::{Executor, HasObservers, StdChildArgs, forkserver::ForkserverExecutor},
    feedbacks::ConstFeedback, fuzzer::StdFuzzer, monitors::SimpleMonitor,
    observers::{StdMapObserver, ObserversTuple}, schedulers::QueueScheduler};
use libafl_bolts::{StdTargetArgs, rands::StdRand, shmem::{ShMem, ShMemProvider, UnixShMemProvider}, tuples::tuple_list};
use crate::{State, input::PtxInput, features, lexer};
pub fn run(seed: &Path, root: &Path) {
    assert!(!root.exists(), "Use a new output directory");
    fs::create_dir_all(root).unwrap(); let root = root.canonicalize().unwrap();
    let input = PtxInput::parse(&fs::read(seed).unwrap()).expect("Marked seed required");
    let compiler = Path::new("/usr/local/cuda-13.0/bin/ptxas");
    let hash = Command::new("sha256sum").arg(compiler).output().unwrap();
    let hash = String::from_utf8(hash.stdout).unwrap().split_whitespace().next().unwrap().to_owned();
    let registry: serde_json::Value = serde_json::from_str(include_str!("../../opcode_registry.json")).unwrap();
    assert_eq!(hash, registry["compiler_sha256"].as_str().unwrap());
    let qemu = Path::new(env!("CARGO_MANIFEST_DIR")).join("../tools/AFLplusplus/afl-qemu-trace").canonicalize().unwrap();
    let mut state = State::new(StdRand::with_seed(1), InMemoryOnDiskCorpus::no_meta(root.join("queue")).unwrap(),
        InMemoryCorpus::new(), &mut ConstFeedback::new(false), &mut ConstFeedback::new(false)).unwrap();
    let mut manager: SimpleEventManager<PtxInput, _, State> = SimpleEventManager::new(SimpleMonitor::new(|_| {}));
    let mut fuzzer = StdFuzzer::new(QueueScheduler::new(), ConstFeedback::new(false), ConstFeedback::new(false));
    let cases = [
        ("empty", " "),
        ("banana", " banana %r2, %r0, %r1; "),
        ("papaya", " papaya %r2, %r0, %r1; "),
        ("renamed", " banana %fruit2, %fruit0, %fruit1; "),
        ("unknown_suffix", " banana.blorp %r2, %r0, %r1; "),
        ("sub_valid", " sub.u32 %r2, %r0, %r1; "),
        ("sub_bad_operands", " sub.u32 %r2; "),
        ("sub_bad_suffix", " sub.blorp %r2, %r0, %r1; "),
        ("rn", " add.rn.f32 %r2, %r0, %r1; "),
        ("rz", " add.rz.f32 %r2, %r0, %r1; "),
        ("rm", " add.rm.f32 %r2, %r0, %r1; "),
        ("rp", " add.rp.f32 %r2, %r0, %r1; "),
        ("rq", " add.rq.f32 %r2, %r0, %r1; "),
        ("rx", " add.rx.f32 %r2, %r0, %r1; "),
        ("bad_type", " add.rn.fbanana %r2, %r0, %r1; "),
    ];
    let mut results=Vec::new();
    for enabled in [false,true] {
        let name=if enabled { "recognition" } else { "baseline" };
        let directory=root.join(name);fs::create_dir(&directory).unwrap();
        let mut sp=UnixShMemProvider::new().unwrap();
        let size=features::EDGES+lexer::feature_offset(enabled);
        let mut map=sp.new_shmem(size).unwrap();unsafe { map.write_to_env("__AFL_SHM_ID").unwrap(); }
        let pointer=map.as_ptr();
        let observer=unsafe { StdMapObserver::new("edges",&mut map[..]) };
        let mut builder=ForkserverExecutor::builder().program(&qemu).arg(compiler)
            .env("AFL_QEMU_MAP_SIZE",features::EDGES.to_string()).env("AFL_ENTRYPOINT","0x4428e0")
            .coverage_map_size(size).shmem_provider(&mut sp).timeout(Duration::from_secs(2))
            .arg("-arch=sm_75").arg("-o").arg(directory.join("current.cubin"))
            .arg_input_file(directory.join("current.ptx"));
        if enabled { builder=builder.env("AFL_QEMU_IJON",lexer::config(&directory)); }
        let mut executor=builder.build(tuple_list!(observer)).unwrap();
        for (suffix, region) in cases {
            let kernel=input.with_region(region.as_bytes().to_vec());
            fs::write(directory.join(format!("{suffix}.ptx")), kernel.bytes()).unwrap();
            for repeat in 0..2 {
                let _=fs::remove_file(directory.join("current.cubin"));
                executor.observers_mut().pre_exec_all(&mut state,&kernel).unwrap();
                let exit=executor.run_target(&mut fuzzer,&mut state,&mut manager,&kernel).unwrap();
                executor.observers_mut().post_exec_all(&mut state,&kernel,&exit).unwrap();
                let snapshot=unsafe { std::slice::from_raw_parts(pointer,size) };
                // Save raw map: repeats check stability; edge counts and IJON presence are separate.
                fs::write(directory.join(format!("{suffix}-{repeat}.map")),snapshot).unwrap();
                results.push(serde_json::json!({"arm":name,"case":suffix,"repeat":repeat,
                    "accepted":directory.join("current.cubin").exists(),"exit":format!("{exit:?}"),
                    "edges":snapshot[..features::EDGES].iter().filter(|&&v|v!=0).count(),
                    "recognitions":snapshot[features::EDGES..features::EDGES+if enabled {lexer::SLOTS}else{0}].iter().filter(|&&v|v!=0).count()}));
            }
        }
    }
    let load = |arm: &str, case: &str, repeat: usize| fs::read(root.join(arm).join(format!("{case}-{repeat}.map"))).unwrap();
    for arm in ["baseline", "recognition"] {
        for (case, _) in cases {
            assert_eq!(load(arm,case,0), load(arm,case,1), "Unstable coverage for {arm}/{case}");
        }
    }
    let recognition = |case: &str| load("recognition",case,0)[features::EDGES..features::EDGES+lexer::SLOTS].to_vec();
    let empty=recognition("empty");
    for case in ["banana", "papaya", "renamed", "unknown_suffix"] {
        assert_eq!(empty, recognition(case), "Arbitrary spelling earned recognition coverage: {case}");
    }
    let known=recognition("sub_bad_operands");
    assert!(empty.iter().zip(&known).any(|(&a,&b)| a==0 && b!=0), "Known opcode with bad operands earned no coverage");
    assert_eq!(known,recognition("sub_valid"), "Operand errors changed recognition of the same opcode/type");
    assert_eq!(recognition("rq"), recognition("rx"), "Unknown suffix spelling earned distinct coverage");
    assert_ne!(recognition("rn"), recognition("rz"), "Rounding modes not distinguished");
    assert_ne!(recognition("rn"), recognition("bad_type"), "Unknown type was rewarded like a recognized type");
    for case in ["rn","rz","rm","rp","sub_valid"] {
        assert!(results.iter().any(|r| r["arm"]=="recognition" && r["case"]==case && r["accepted"]==true), "Valid fixture rejected: {case}");
    }
    for case in ["banana","papaya","unknown_suffix","sub_bad_operands","rq","rx","bad_type"] {
        assert!(results.iter().any(|r| r["arm"]=="recognition" && r["case"]==case && r["accepted"]==false), "Invalid fixture accepted: {case}");
    }
    fs::write(root.join("summary.json"),serde_json::to_vec_pretty(&serde_json::json!({"compiler_sha256":hash,
        "mode":"registered-opcodes-and-reserved-tokens", "checks":{"repeat_maps_identical":true,
        "arbitrary_names_no_new_recognition":true,"known_opcode_bad_operands_rewarded":true,
        "unknown_suffixes_no_distinct_recognition":true,"recognized_rounding_modes_distinguished":true,
        "unknown_type_not_rewarded":true},"runs":results})).unwrap()).unwrap();
}
