//! Hash-verified swap of a reversible client bundle overlay, as prepared by
//! `Scripts/patch_local_store.py` (manifest.json, original.bundle, patched.bundle).
use crate::install;
use anyhow::{bail, Context, Result};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Original,
    Patched,
}

#[derive(Deserialize)]
struct Manifest {
    source: PathBuf,
    original_sha256: String,
    patched_sha256: String,
}

struct Overlay {
    target: PathBuf,
    original: String,
    patched: String,
    dir: PathBuf,
}

impl Overlay {
    fn load(dir: &Path, game: &Path) -> Result<Self> {
        let path = dir.join("manifest.json");
        let manifest: Manifest = serde_json::from_slice(
            &fs::read(&path).with_context(|| format!("reading {}", path.display()))?,
        )
        .with_context(|| format!("parsing {}", path.display()))?;
        let game = fs::canonicalize(game)
            .with_context(|| format!("invalid game directory: {}", game.display()))?;
        let target = fs::canonicalize(&manifest.source).with_context(|| {
            format!("overlay target is missing: {}", manifest.source.display())
        })?;
        if !target.starts_with(&game) || !target.is_file() {
            bail!("overlay target lies outside the selected game directory");
        }
        Ok(Self {
            target,
            original: manifest.original_sha256.to_ascii_lowercase(),
            patched: manifest.patched_sha256.to_ascii_lowercase(),
            dir: dir.to_path_buf(),
        })
    }

    fn side(&self) -> Result<Side> {
        let actual = sha256(&fs::read(&self.target)?);
        if actual == self.original {
            Ok(Side::Original)
        } else if actual == self.patched {
            Ok(Side::Patched)
        } else {
            bail!(
                "client bundle changed since the store overlay was prepared; re-prepare it: {}",
                self.target.display()
            )
        }
    }
}

/// Which side of the overlay is installed; fails if the bundle matches neither.
pub fn inspect(dir: &Path, game: &Path) -> Result<Side> {
    Overlay::load(dir, game)?.side()
}

/// Installs `wanted`. Returns whether the bundle was changed.
pub fn switch(dir: &Path, game: &Path, wanted: Side) -> Result<bool> {
    if install::game_running()? {
        bail!("close PGR before switching the client bundle");
    }
    let overlay = Overlay::load(dir, game)?;
    if overlay.side()? == wanted {
        return Ok(false);
    }
    let (file, expected) = match wanted {
        Side::Original => ("original.bundle", &overlay.original),
        Side::Patched => ("patched.bundle", &overlay.patched),
    };
    let payload = fs::read(overlay.dir.join(file))
        .with_context(|| format!("reading overlay {file}"))?;
    if &sha256(&payload) != expected {
        bail!("overlay {file} does not match its manifest checksum");
    }
    let mut temporary = overlay.target.clone().into_os_string();
    temporary.push(".ascnet-overlay.tmp");
    let temporary = PathBuf::from(temporary);
    fs::write(&temporary, &payload)?;
    fs::rename(&temporary, &overlay.target).inspect_err(|_| {
        let _ = fs::remove_file(&temporary);
    })?;
    if &sha256(&fs::read(&overlay.target)?) != expected {
        bail!("client bundle checksum mismatch after switching overlay");
    }
    Ok(true)
}

fn sha256(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> (PathBuf, PathBuf, PathBuf) {
        let root = std::env::temp_dir().join(format!("ascnet-overlay-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let game = root.join("game");
        let dir = root.join("overlay");
        fs::create_dir_all(game.join("matrix")).unwrap();
        fs::create_dir_all(&dir).unwrap();
        let target = game.join("matrix/bundle.uab");
        fs::write(&target, b"stock").unwrap();
        fs::write(dir.join("original.bundle"), b"stock").unwrap();
        fs::write(dir.join("patched.bundle"), b"store").unwrap();
        fs::write(
            dir.join("manifest.json"),
            serde_json::json!({
                "source": target,
                "original_sha256": sha256(b"stock"),
                "patched_sha256": sha256(b"store"),
                "changed_scripts": [],
            })
            .to_string(),
        )
        .unwrap();
        (root, game, dir)
    }

    #[test]
    fn switches_both_ways_and_is_idempotent() {
        let (root, game, dir) = fixture("swap");
        let target = game.join("matrix/bundle.uab");
        assert_eq!(inspect(&dir, &game).unwrap(), Side::Original);
        assert!(!switch(&dir, &game, Side::Original).unwrap());
        assert!(switch(&dir, &game, Side::Patched).unwrap());
        assert_eq!(fs::read(&target).unwrap(), b"store");
        assert!(switch(&dir, &game, Side::Original).unwrap());
        assert_eq!(fs::read(&target).unwrap(), b"stock");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn refuses_unknown_bundle_or_corrupt_payload() {
        let (root, game, dir) = fixture("refuse");
        let target = game.join("matrix/bundle.uab");
        fs::write(dir.join("patched.bundle"), b"tampered").unwrap();
        assert!(switch(&dir, &game, Side::Patched).is_err());
        assert_eq!(fs::read(&target).unwrap(), b"stock");
        fs::write(&target, b"fps-tweaked").unwrap();
        assert!(inspect(&dir, &game).is_err());
        assert!(switch(&dir, &game, Side::Original).is_err());
        assert_eq!(fs::read(&target).unwrap(), b"fps-tweaked");
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn refuses_target_outside_game() {
        let (root, game, dir) = fixture("outside");
        let other = root.join("other");
        fs::create_dir_all(&other).unwrap();
        assert!(inspect(&dir, &other).is_err());
        assert!(inspect(&dir, &game).is_ok());
        fs::remove_dir_all(root).unwrap();
    }
}
