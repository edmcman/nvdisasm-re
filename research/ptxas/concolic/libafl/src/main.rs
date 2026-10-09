//! Local compiler lowering enumeration. No crash or vulnerability objective.
use std::{borrow::Cow, collections::HashSet, env, fs, io::{BufRead, Write}, os::unix::net::UnixStream,
          path::PathBuf, process::Command, sync::mpsc, time::{Duration, Instant}};
use libafl::{Error, HasMetadata, corpus::{Corpus, HasCurrentCorpusId, InMemoryCorpus, InMemoryOnDiskCorpus}, events::SimpleEventManager,
    executors::{ExitKind, StdChildArgs, forkserver::ForkserverExecutor},
    feedback_or, feedbacks::{ConstFeedback, MaxMapFeedback, TimeFeedback}, fuzzer::{Evaluator, Fuzzer, StdFuzzer},
    inputs::{HasMutatorBytes, HasTargetBytes}, monitors::SimpleMonitor,
    mutators::{BytesDeleteMutator, HavocScheduledMutator, MutationResult, Mutator, Tokens, havoc_mutations, tokens_mutations},
    observers::{CanTrack, HitcountsMapObserver, Observer, StdMapObserver, TimeObserver},
    schedulers::{IndexesLenTimeMinimizerScheduler, StdWeightedScheduler, powersched::PowerSchedule},
    stages::{ClosureStage, IfStage, ObserverEqualityFactory, StdTMinMutationalStage, calibrate::CalibrationStage,
             power::StdPowerMutationalStage},
    state::{HasCorpus, HasCurrentTestcase, HasExecutions, StdState}};
use libafl_bolts::{Named, StdTargetArgs, current_nanos, rands::StdRand,
    shmem::{ShMem, ShMemProvider, UnixShMemProvider}, tuples::{Merge, tuple_list},
    llmp::{LlmpConnection, LlmpClient, Tag}};
mod features;
mod input;
use input::PtxInput;

static STOP: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
extern "C" fn stop_worker(_: libc::c_int) { STOP.store(true, std::sync::atomic::Ordering::Relaxed); }
const TAG: Tag = Tag(0x505458);

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.get(1).is_some_and(|x| x == "--broker") {
        let sp = UnixShMemProvider::new().unwrap();
        if let LlmpConnection::IsBroker { mut broker } = LlmpConnection::broker_on_port(sp, args[2].parse().unwrap()).unwrap() {
            println!("ready"); std::io::stdout().flush().unwrap();
            broker.loop_forever(Some(Duration::from_millis(5)));
        }
    } else if args.get(1).is_some_and(|x| x == "--publish") {
        let mut client = LlmpClient::create_attach_to_tcp(UnixShMemProvider::new().unwrap(), args[2].parse().unwrap()).unwrap();
        for line in std::io::stdin().lock().lines() {
            client.send_buf(TAG, line.unwrap().as_bytes()).unwrap();
        }
    } else if args.get(1).is_some_and(|x| x == "--region") {
        // Mutation region of a file, for checking agreement with corpus.instruction_region.
        match PtxInput::parse(&fs::read(&args[2]).unwrap()).map(|p| p.span()) {
            Some((begin, end)) => println!("{begin} {end}"),
            None => println!("none"),
        }
    } else if args.get(1).is_some_and(|x| x == "--worker") {
        worker(&args[2..]);
    } else {
        // The Python service integrates the repository's md decoder and GDB IR loggers.
        let service = PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap().join("pipeline.py");
        let status = Command::new("python3").arg(service).arg("--application").arg(env::current_exe().unwrap()).args(&args[1..]).status().unwrap();
        std::process::exit(status.code().unwrap_or(1));
    }
}

/// ptxas writes its -o file only on success. Every successful compilation, including
/// those run inside the trim stage, is submitted once per epoch. The run's PTX and SASS facts
/// are marked in the coverage map's feature slots (see features.rs).
#[derive(serde::Serialize, serde::Deserialize)]
struct Compiled { output: PathBuf, origin: String, #[serde(skip)] tx: Option<mpsc::Sender<serde_json::Value>>,
                  #[serde(skip)] submitted: HashSet<Vec<u8>>, #[serde(skip)] map: Option<std::ptr::NonNull<u8>> }
impl Named for Compiled {
    fn name(&self) -> &Cow<'static, str> { static NAME: Cow<'static, str> = Cow::Borrowed("compiled"); &NAME }
}
impl<I: HasTargetBytes + HasMutatorBytes, S> Observer<I, S> for Compiled {
    fn pre_exec(&mut self, _: &mut S, _: &I) -> Result<(), Error> { let _ = fs::remove_file(&self.output); Ok(()) }
    fn post_exec(&mut self, _: &mut S, input: &I, _: &ExitKind) -> Result<(), Error> {
        let bytes = input.target_bytes();
        let cubin = fs::read(&self.output).ok();
        if let Some(tx) = &self.tx && cubin.is_some() && self.submitted.insert(bytes.to_vec()) {
            let _ = tx.send(serde_json::json!({"kind":"candidate", "origin":self.origin, "data":&*bytes}));
        }
        if let (Some(map), Some(cubin)) = (self.map, &cubin) {
            let slots = features::ptx(input.mutator_bytes()).into_iter().chain(features::sass(cubin));
            // The edge observer has already reset and classified this shared map for the run;
            // feature slots lie past the qemu edges, so only this observer writes them.
            for slot in slots { unsafe { *map.as_ptr().add(slot) = 1; } }
        }
        Ok(())
    }
}

type State = StdState<InMemoryOnDiskCorpus<PtxInput>, PtxInput, StdRand, InMemoryCorpus<PtxInput>>;

/// AFL++ trims an entry when it is first fuzzed.
fn first_scheduling<Z, E, EM>(_: &mut Z, _: &mut E, state: &mut State, _: &mut EM) -> Result<bool, Error> {
    Ok(state.current_testcase()?.scheduled_count() == 0)
}

/// LibAFL's minimizer retries a skipped mutation without counting it, and the delete mutator
/// skips inputs of at most two bytes, so a trimmed-down region would loop forever. Reporting the
/// unchanged input as mutated keeps the attempt count bounded (one redundant execution).
struct Counted<M>(M);
impl<M: Named> Named for Counted<M> { fn name(&self) -> &Cow<'static, str> { self.0.name() } }
impl<I, S, M: Mutator<I, S>> Mutator<I, S> for Counted<M> {
    fn mutate(&mut self, state: &mut S, input: &mut I) -> Result<MutationResult, Error> {
        self.0.mutate(state, input).map(|_| MutationResult::Mutated)
    }
    fn post_exec(&mut self, state: &mut S, id: Option<libafl::corpus::CorpusId>) -> Result<(), Error> { self.0.post_exec(state, id) }
}

/// Minimization attempts per entry (LibAFL's counterpart of AFL's trim).
const TRIM_RUNS: usize = 64;
/// HAVOC_STACK_POW2 in AFL++ config.h: 2..16 stacked mutations.
const HAVOC_STACK_POW2: usize = 4;

fn worker(args: &[String]) {
    unsafe { libc::signal(libc::SIGUSR1, stop_worker as *const () as libc::sighandler_t); }
    // root, index, broker port, qemu, compiler, comma-separated dictionaries, comma-separated targets.
    // The coordinator owns the campaign deadline and stops workers with SIGUSR1.
    let root = PathBuf::from(&args[0]); let index = &args[1];
    let stopped = || STOP.load(std::sync::atomic::Ordering::Relaxed);
    let mut client = LlmpClient::create_attach_to_tcp(UnixShMemProvider::new().unwrap(), args[2].parse().unwrap()).unwrap();
    let (tx, rx) = mpsc::channel::<serde_json::Value>();
    let socket = root.join("candidates.sock");
    std::thread::spawn(move || {
        let mut stream = UnixStream::connect(socket).unwrap();
        for msg in rx { if writeln!(stream, "{msg}").is_err() { break; } }
    });
    let targets: Vec<_> = args[6].split(',').collect();
    let worker_dir = root.join("mutation").join(index); fs::create_dir_all(&worker_dir).unwrap();
    let mut epoch = 0; let mut total = 0u64;
    let mut broadcasts = Vec::<(String, PathBuf)>::new(); let mut imported = HashSet::new();
    while !stopped() {
        let arch = targets[(index.parse::<usize>().unwrap() + epoch) % targets.len()];
        let checkpoint = worker_dir.join(format!("{arch}.state"));
        let mut sp = UnixShMemProvider::new().unwrap();
        let mut shmem = sp.new_shmem(features::EDGES + features::SLOTS).unwrap();
        let map = std::ptr::NonNull::new(shmem.as_mut_ptr());
        unsafe { shmem.write_to_env("__AFL_SHM_ID").unwrap(); }
        let edges = unsafe { HitcountsMapObserver::new(StdMapObserver::new("edges", &mut shmem[..])) }.track_indices();
        let time = TimeObserver::new("time");
        let map_feedback = MaxMapFeedback::new(&edges);
        let calibration = CalibrationStage::new(&map_feedback);
        let mut feedback = feedback_or!(map_feedback, TimeFeedback::new(&time));
        let mut objective = ConstFeedback::new(false);
        let mut state: State = if checkpoint.exists() {
            postcard::from_bytes(&fs::read(&checkpoint).unwrap()).expect("restore architecture state")
        } else { // The coverage corpus, rejected inputs included, is kept on disk for inspection.
                 StdState::new(StdRand::with_seed(current_nanos()), InMemoryOnDiskCorpus::no_meta(worker_dir.join("queue").join(arch)).unwrap(),
                   InMemoryCorpus::<PtxInput>::new(), &mut feedback, &mut objective).unwrap() };
        if !state.has_metadata::<Tokens>() {
            state.add_metadata(Tokens::new().add_from_files(args[5].split(',')).unwrap());
        }
        let mut mgr = SimpleEventManager::new(SimpleMonitor::new(|_| {}));
        // AFL++ queue policy: favored minimal inputs per edge, power-scheduled energy. Rejected
        // inputs stay in the corpus but compete with compiling ones that cover the backend.
        // Keep every entry's edge indexes: trim replaces entries and re-scores them against all others.
        let scheduler = IndexesLenTimeMinimizerScheduler::non_metadata_removing(&edges,
            StdWeightedScheduler::with_schedule(&mut state, &edges, Some(PowerSchedule::explore())));
        let mut fuzzer = StdFuzzer::new(scheduler, feedback, objective);
        // Trim: delete region bytes while the edge map is unchanged, replacing the entry.
        let trim = StdTMinMutationalStage::new(Counted(BytesDeleteMutator::new()), ObserverEqualityFactory::new(&edges), TRIM_RUNS);
        let compiled = Compiled { output: worker_dir.join("current.cubin"), origin: format!("mutation:{index}:{arch}"),
                                  tx: Some(tx.clone()), submitted: HashSet::new(), map };
        let ptxas = |output: &PathBuf| ForkserverExecutor::builder().program(&args[3])
            .env("AFL_QEMU_MAP_SIZE", features::EDGES.to_string())
            .coverage_map_size(features::EDGES + features::SLOTS).arg(&args[4]).arg(format!("-arch={arch}")).arg("-o").arg(output);
        let mut executor = ptxas(&compiled.output).shmem_provider(&mut sp).timeout(Duration::from_secs(2))
            .arg_input_file(worker_dir.join("current.ptx")).build(tuple_list!(edges, time, compiled)).unwrap();
        if state.corpus().count() == 0 {
            // Every marked seed is queued, as AFL++ does; files without markers are not mutated.
            for seed in fs::read_dir(root.join("seeds")).unwrap() {
                if let Some(input) = PtxInput::parse(&fs::read(seed.unwrap().path()).unwrap()) {
                    fuzzer.add_input(&mut state, &mut executor, &mut mgr, input).unwrap();
                }
            }
        }
        let havoc = StdPowerMutationalStage::new(HavocScheduledMutator::with_max_stack_pow(havoc_mutations().merge(tokens_mutations()), HAVOC_STACK_POW2));
        // LibAFL's minimizer records the replacement as its own parent, which makes calibration
        // re-borrow the entry while it holds it; an entry trimmed in place has no parent.
        let orphan = ClosureStage::new(|_: &mut _, _: &mut _, state: &mut State, _: &mut _| -> Result<(), Error> {
            let id = state.current_corpus_id()?;
            let mut testcase = state.current_testcase_mut()?;
            if testcase.parent_id() == id { testcase.set_parent_id_optional(None); }
            Ok(())
        });
        // Trim first; calibration then measures the replacement and gives it scheduler metadata.
        let mut stages = tuple_list!(IfStage::new(first_scheduling, tuple_list!(trim, orphan)), calibration, havoc);
        let epoch_start = Instant::now(); let mut saved = Instant::now(); let mut reported = Instant::now();
        while epoch_start.elapsed() < Duration::from_secs(60) && !stopped() {
            while let Some((_, tag, bytes)) = client.recv_buf().unwrap() {
                if tag == TAG { let msg: (String, PathBuf) = serde_json::from_slice(bytes).unwrap(); broadcasts.push(msg); }
            }
            for (target, path) in &broadcasts {
                if target == arch && imported.insert((target.clone(), path.clone())) {
                    // Accepted cases enter scheduling even without new compiler coverage; executing
                    // them records the coverage metadata the minimizing scheduler needs.
                    if let Some(input) = PtxInput::parse(&fs::read(path).unwrap()) {
                        fuzzer.add_input(&mut state, &mut executor, &mut mgr, input).unwrap();
                    }
                }
            }
            if state.corpus().count() == 0 { std::thread::sleep(Duration::from_millis(100)); continue; }
            let before = *state.executions();
            // SIGUSR1 interrupts the forkserver wait; that is a stop request, not a failure.
            if let Err(e) = fuzzer.fuzz_one(&mut stages, &mut executor, &mut state, &mut mgr) {
                if stopped() { break; }
                panic!("{e:?}");
            }
            total += *state.executions() - before;
            if reported.elapsed() >= Duration::from_secs(1) {
                tx.send(serde_json::json!({"kind":"activity", "worker":index, "arch":arch, "executions":total, "context_executions":state.executions(), "epoch":epoch})).unwrap();
                reported = Instant::now();
            }
            if saved.elapsed() >= Duration::from_secs(5) {
                fs::write(checkpoint.with_extension("tmp"), postcard::to_allocvec(&state).unwrap()).unwrap();
                fs::rename(checkpoint.with_extension("tmp"), &checkpoint).unwrap(); saved=Instant::now();
            }
        }
        fs::write(&checkpoint, postcard::to_allocvec(&state).unwrap()).unwrap();
        epoch += 1;
    }
}
