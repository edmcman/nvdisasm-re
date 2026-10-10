//! Compile every one-token substitution of a seed with real ptxas (no fuzzing, no QEMU),
//! for the seed's own `.target`, and report accepted spellings per class.
use std::{collections::BTreeMap, fs, path::Path, process::Command};
use crate::{input::PtxInput, tokens::Vocabulary};

pub fn run(seed: &Path, root: &Path) {
    assert!(!root.exists(), "Probe output already exists");
    fs::create_dir_all(root).unwrap();
    let compiler = Path::new("/usr/local/cuda-13.0/bin/ptxas");
    let data = fs::read(seed).unwrap();
    let input = PtxInput::parse(&data).expect("Instruction markers required");
    let target = String::from_utf8_lossy(&data).lines().find_map(|l| l.strip_prefix(".target ")).expect(".target required").trim().to_owned();
    let vocabulary = Vocabulary::load(compiler);
    let compile = |path: &Path| Command::new(compiler).arg(format!("-arch={target}")).args(["-o", "/dev/null"]).arg(path).output().unwrap();
    fs::write(root.join("seed.ptx"), input.bytes()).unwrap();
    assert!(compile(&root.join("seed.ptx")).status.success(), "Seed must compile");
    let mut classes = BTreeMap::<String, (usize, Vec<String>)>::new();
    for (i, (class, region)) in vocabulary.candidates(&input.region).into_iter().enumerate() {
        let path = root.join(format!("{i:04}.ptx"));
        fs::write(&path, input.with_region(region.clone()).bytes()).unwrap();
        let entry = classes.entry(vocabulary.labels[class].clone()).or_default();
        entry.0 += 1;
        if compile(&path).status.success() { entry.1.push(String::from_utf8_lossy(&region).trim().to_owned()); }
    }
    let summary = serde_json::json!({"seed": seed, "target": target, "region": String::from_utf8_lossy(&input.region),
        "classes": classes.iter().map(|(label, (n, accepted))| (label.clone(), serde_json::json!({"candidates": n, "accepted": accepted})))
            .collect::<serde_json::Map<_, _>>()});
    fs::write(root.join("summary.json"), serde_json::to_vec_pretty(&summary).unwrap()).unwrap();
    for (label, (n, accepted)) in &classes { println!("{label}: {n} candidates, {} accepted", accepted.len()); }
}
