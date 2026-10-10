//! IJON feedback for registered opcodes and dedicated reserved-token actions.
use std::{fs, path::{Path, PathBuf}};
pub const SLOTS: usize = 65536;
pub const MAX_BYTES: usize = 4096; // Unused QEMU max/min reservation.
pub const BYTES: usize = SLOTS + MAX_BYTES;
pub fn config(directory: &Path) -> PathBuf {
    let path = directory.join("parser-recognition.ijon");
    fs::write(&path, include_str!("../../parser-recognition.ijon")).unwrap();
    path
}
pub fn feature_offset(enabled: bool) -> usize { if enabled { BYTES } else { 0 } }
// Retain the old metadata type so old disabled checkpoints deserialize. Active
// transition-feedback checkpoints must be rejected, since their slot meanings differ.
#[derive(Debug, serde::Serialize, serde::Deserialize, libafl_bolts::SerdeAny)]
pub struct Config(pub bool);
#[derive(Debug, serde::Serialize, serde::Deserialize, libafl_bolts::SerdeAny)]
pub struct RecognitionConfig(pub bool);
