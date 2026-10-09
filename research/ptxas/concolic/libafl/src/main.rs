//! Local compiler lowering enumeration. No crash or vulnerability objective.
use std::{borrow::Cow, collections::HashSet, env, fs, io::{BufRead, Write}, os::unix::net::UnixStream,
          path::PathBuf, process::Command, sync::mpsc, time::{Duration, Instant}};
use libafl::{Error, HasMetadata, corpus::{Corpus, InMemoryCorpus, InMemoryOnDiskCorpus, Testcase}, events::SimpleEventManager,
    executors::{ExitKind, StdChildArgs, forkserver::{ForkserverExecutor, SHM_CMPLOG_ENV_VAR}},
    feedbacks::{ConstFeedback, MaxMapFeedback}, fuzzer::{Fuzzer, HasScheduler, StdFuzzer},
    inputs::{BytesInput, HasTargetBytes}, monitors::SimpleMonitor,
    mutators::{HavocScheduledMutator, Tokens, havoc_mutations, tokens_mutations, token_mutations::AflppRedQueen},
    observers::{HitcountsMapObserver, Observer, StdMapObserver}, schedulers::{Scheduler, QueueScheduler},
    stages::{ColorizationStage, IfStage, StdMutationalStage, mutational::MultiMutationalStage},
    state::{HasCorpus, HasCurrentTestcase, HasExecutions, StdState}};
use libafl_bolts::{Named, StdTargetArgs, current_nanos, rands::StdRand,
    shmem::{ShMem, ShMemProvider, UnixShMemProvider}, tuples::{Handled, Merge, tuple_list},
    llmp::{LlmpConnection, LlmpClient, Tag}};
use libafl_targets::{AflppCmpLogMap, cmps::{observers::AflppCmpLogObserver, stages::AflppCmplogTracingStage}};
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
/// those run inside colorization and RedQueen stages, is submitted once per epoch.
#[derive(serde::Serialize, serde::Deserialize)]
struct Compiled { output: PathBuf, origin: String, #[serde(skip)] tx: Option<mpsc::Sender<serde_json::Value>>,
                  #[serde(skip)] submitted: HashSet<Vec<u8>> }
impl Named for Compiled {
    fn name(&self) -> &Cow<'static, str> { static NAME: Cow<'static, str> = Cow::Borrowed("compiled"); &NAME }
}
impl<S> Observer<BytesInput, S> for Compiled {
    fn pre_exec(&mut self, _: &mut S, _: &BytesInput) -> Result<(), Error> { let _ = fs::remove_file(&self.output); Ok(()) }
    fn post_exec(&mut self, _: &mut S, input: &BytesInput, _: &ExitKind) -> Result<(), Error> {
        let bytes = input.target_bytes();
        if let Some(tx) = &self.tx && self.output.exists() && self.submitted.insert(bytes.to_vec()) {
            let _ = tx.send(serde_json::json!({"kind":"candidate", "origin":self.origin, "data":&*bytes}));
        }
        Ok(())
    }
}

type State = StdState<InMemoryOnDiskCorpus<BytesInput>, BytesInput, StdRand, InMemoryCorpus<BytesInput>>;

fn worker(args: &[String]) {
    unsafe { libc::signal(libc::SIGUSR1, stop_worker as *const () as libc::sighandler_t); }
    // root, index, broker port, qemu, compiler, comma-separated dictionaries, comma-separated targets, cmplog (0/1).
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
    let targets: Vec<_> = args[6].split(',').collect(); let cmplog = args[7] == "1";
    let worker_dir = root.join("mutation").join(index); fs::create_dir_all(&worker_dir).unwrap();
    let mut epoch = 0; let mut total = 0u64;
    let mut broadcasts = Vec::<(String, PathBuf)>::new();
    while !stopped() {
        let arch = targets[(index.parse::<usize>().unwrap() + epoch) % targets.len()];
        let checkpoint = worker_dir.join(format!("{arch}.state"));
        let mut sp = UnixShMemProvider::new().unwrap();
        let mut shmem = sp.new_shmem(1048576).unwrap();
        unsafe { shmem.write_to_env("__AFL_SHM_ID").unwrap(); }
        let edges = unsafe { HitcountsMapObserver::new(StdMapObserver::new("edges", &mut shmem[..])) };
        let mut feedback = MaxMapFeedback::new(&edges);
        let mut objective = ConstFeedback::new(false);
        let mut state: State = if checkpoint.exists() {
            postcard::from_bytes(&fs::read(&checkpoint).unwrap()).expect("restore architecture state")
        } else { // The coverage corpus, rejected inputs included, is kept on disk for inspection.
                 StdState::new(StdRand::with_seed(current_nanos()), InMemoryOnDiskCorpus::no_meta(worker_dir.join("queue").join(arch)).unwrap(),
                   InMemoryCorpus::<BytesInput>::new(), &mut feedback, &mut objective).unwrap() };
        if !state.has_metadata::<Tokens>() {
            state.add_metadata(Tokens::new().add_from_files(args[5].split(',')).unwrap());
        }
        let mut mgr = SimpleEventManager::new(SimpleMonitor::new(|_| {}));
        let mut fuzzer = StdFuzzer::new(QueueScheduler::new(), feedback, objective);
        let colorization = ColorizationStage::new(&edges);
        let compiled = Compiled { output: worker_dir.join("current.cubin"), origin: format!("mutation:{index}:{arch}"),
                                  tx: Some(tx.clone()), submitted: HashSet::new() };
        let ptxas = |output: &PathBuf| ForkserverExecutor::builder().program(&args[3])
            .env("AFL_MAP_SIZE", "1048576").env("AFL_QEMU_MAP_SIZE", "1048576")
            .coverage_map_size(1048576).arg(&args[4]).arg(format!("-arch={arch}")).arg("-o").arg(output);
        let mut executor = ptxas(&compiled.output).shmem_provider(&mut sp).timeout(Duration::from_secs(2))
            .arg_input_file(worker_dir.join("current.ptx")).build(tuple_list!(edges, compiled)).unwrap();
        if state.corpus().count() == 0 {
            state.load_initial_inputs(&mut fuzzer, &mut executor, &mut mgr, &[root.join("seeds")]).unwrap();
        }
        let havoc = || StdMutationalStage::new(HavocScheduledMutator::with_max_stack_pow(havoc_mutations().merge(tokens_mutations()), 3));
        // Stage tuples differ in type with and without CmpLog, so the epoch loop is a macro.
        macro_rules! fuzz_epoch { ($stages:ident) => {{
        let epoch_start = Instant::now(); let mut saved = Instant::now(); let mut reported = Instant::now();
        let mut imported = HashSet::new();
        while epoch_start.elapsed() < Duration::from_secs(60) && !stopped() {
            while let Some((_, tag, bytes)) = client.recv_buf().unwrap() {
                if tag == TAG { let msg: (String, PathBuf) = serde_json::from_slice(bytes).unwrap(); broadcasts.push(msg); }
            }
            for (target, path) in &broadcasts {
                if target == arch && imported.insert(path.clone()) {
                    // Accepted cases enter scheduling even without new compiler coverage.
                    let id = state.corpus_mut().add(Testcase::new(BytesInput::new(fs::read(path).unwrap()))).unwrap();
                    HasScheduler::<BytesInput, State>::scheduler_mut(&mut fuzzer).on_add(&mut state, id).unwrap();
                }
            }
            let before = *state.executions();
            // SIGUSR1 interrupts the forkserver wait; that is a stop request, not a failure.
            if let Err(e) = fuzzer.fuzz_one(&mut $stages, &mut executor, &mut state, &mut mgr) {
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
        }}; }
        if cmplog {
            // Only the tracer logs comparisons; its forkserver reads the environment when it starts.
            let mut cmplog_shmem = sp.new_shmem(size_of::<AflppCmpLogMap>()).unwrap();
            unsafe { cmplog_shmem.write_to_env(SHM_CMPLOG_ENV_VAR).unwrap(); }
            let observer = AflppCmpLogObserver::new("cmplog", unsafe { AflppCmpLogMap::from_shmem(&mut cmplog_shmem) }, true);
            let handle = observer.handle();
            let tracer = ptxas(&PathBuf::from("/dev/null")).shmem_provider(&mut sp).timeout(Duration::from_secs(20))
                .arg_input_file(worker_dir.join("traced.ptx")).build(tuple_list!(observer)).unwrap();
            unsafe { env::remove_var(SHM_CMPLOG_ENV_VAR); }
            // RedQueen once per entry, on its first scheduling as in AFL++; campaigns are too short to
            // revisit entries (LibAFL's example waits for the second).
            let first = |_: &mut _, _: &mut _, state: &mut State, _: &mut _| -> Result<bool, Error> {
                Ok(state.current_testcase()?.scheduled_count() == 0)
            };
            let mut stages = tuple_list!(IfStage::new(first, tuple_list!(colorization, AflppCmplogTracingStage::new(tracer, handle),
                MultiMutationalStage::new(AflppRedQueen::with_cmplog_options(true, true)))), havoc());
            fuzz_epoch!(stages);
        } else {
            let mut stages = tuple_list!(havoc());
            fuzz_epoch!(stages);
        }
        fs::write(&checkpoint, postcard::to_allocvec(&state).unwrap()).unwrap();
        epoch += 1;
    }
}
