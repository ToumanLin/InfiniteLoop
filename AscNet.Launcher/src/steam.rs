use anyhow::{Context, Result};
use std::{
    collections::BTreeMap,
    env, fs,
    path::{Path, PathBuf},
};

pub fn discover_game() -> Result<Option<PathBuf>> {
    for root in steam_roots() {
        for library in libraries(&root) {
            let apps = library.join("steamapps");
            let Ok(entries) = fs::read_dir(&apps) else {
                continue;
            };
            for entry in entries.flatten() {
                let name = entry.file_name();
                let name = name.to_string_lossy();
                if !name.starts_with("appmanifest_") || !name.ends_with(".acf") {
                    continue;
                }
                let Ok(text) = fs::read_to_string(entry.path()) else {
                    continue;
                };
                let Ok(fields) = object_fields(&text, "AppState") else {
                    continue;
                };
                let Some(dir) = fields.get("installdir") else {
                    continue;
                };
                let candidate = apps.join("common").join(dir);
                if valid_game_directory(&candidate) {
                    return Ok(Some(candidate));
                }
            }
        }
    }
    Ok(None)
}

/// Resolves the Steam app id from the manifest of the library that owns `game`.
pub fn app_id(game: &Path) -> Result<Option<String>> {
    // The selected folder may be a link into the Steam library.
    let game = fs::canonicalize(game)
        .with_context(|| format!("invalid game directory: {}", game.display()))?;
    let (Some(common), Some(name)) = (game.parent(), game.file_name()) else {
        return Ok(None);
    };
    if !common
        .file_name()
        .is_some_and(|n| n.eq_ignore_ascii_case("common"))
    {
        return Ok(None);
    }
    let Some(apps) = common.parent() else {
        return Ok(None);
    };
    let Ok(entries) = fs::read_dir(apps) else {
        return Ok(None);
    };
    let name = name.to_string_lossy();
    for entry in entries.flatten() {
        let file = entry.file_name();
        let file = file.to_string_lossy();
        if !file.starts_with("appmanifest_") || !file.ends_with(".acf") {
            continue;
        }
        let Ok(text) = fs::read_to_string(entry.path()) else {
            continue;
        };
        if let Some(id) = manifest_app_id(&text, &name) {
            return Ok(Some(id));
        }
    }
    Ok(None)
}

fn manifest_app_id(text: &str, install_dir: &str) -> Option<String> {
    let fields = object_fields(text, "AppState").ok()?;
    if !fields.get("installdir")?.eq_ignore_ascii_case(install_dir) {
        return None;
    }
    let id = fields.get("appid")?;
    (!id.is_empty() && id.bytes().all(|b| b.is_ascii_digit())).then(|| id.clone())
}

pub fn valid_game_directory(path: &Path) -> bool {
    path.is_dir() && path.join("PGR.exe").is_file()
}

fn steam_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(path) = env::var_os("STEAM_PATH") {
        roots.push(path.into());
    }
    if let Some(path) = env::var_os("PROGRAMFILES(X86)") {
        roots.push(PathBuf::from(path).join("Steam"));
    }
    if let Some(path) = env::var_os("PROGRAMFILES") {
        roots.push(PathBuf::from(path).join("Steam"));
    }
    roots.sort();
    roots.dedup();
    roots
}

fn libraries(root: &Path) -> Vec<PathBuf> {
    let mut result = vec![root.to_path_buf()];
    let file = root.join("steamapps/libraryfolders.vdf");
    let Ok(text) = fs::read_to_string(file) else {
        return result;
    };
    if let Ok(fields) = object_fields(&text, "libraryfolders") {
        for value in fields.values() {
            let path = PathBuf::from(value);
            if path.join("steamapps").is_dir() {
                result.push(path);
            }
        }
    }
    // Modern VDF nests each library's path in a numbered object.
    let tokens = tokens(&text).unwrap_or_default();
    for pair in tokens.windows(2) {
        if pair[0].eq_ignore_ascii_case("path") {
            let path = PathBuf::from(&pair[1]);
            if path.join("steamapps").is_dir() {
                result.push(path);
            }
        }
    }
    result.sort();
    result.dedup();
    result
}

fn object_fields(text: &str, object: &str) -> Result<BTreeMap<String, String>> {
    let t = tokens(text)?;
    let start = t
        .iter()
        .position(|v| v.eq_ignore_ascii_case(object))
        .with_context(|| format!("missing VDF object {object}"))?;
    let mut fields = BTreeMap::new();
    let mut i = start + 1;
    if t.get(i).map(String::as_str) != Some("{") {
        anyhow::bail!("invalid VDF object {object}")
    }
    i += 1;
    let mut depth = 1;
    while i < t.len() && depth > 0 {
        match t[i].as_str() {
            "{" => {
                depth += 1;
                i += 1;
            }
            "}" => {
                depth -= 1;
                i += 1;
            }
            key if depth == 1 && t.get(i + 1).is_some_and(|v| v != "{" && v != "}") => {
                fields.insert(key.to_ascii_lowercase(), t[i + 1].clone());
                i += 2;
            }
            _ => i += 1,
        }
    }
    if depth != 0 {
        anyhow::bail!("unterminated VDF object {object}")
    }
    Ok(fields)
}

fn tokens(text: &str) -> Result<Vec<String>> {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        while i < bytes.len() && bytes[i].is_ascii_whitespace() {
            i += 1;
        }
        if i >= bytes.len() {
            break;
        }
        if bytes[i] == b'/' && bytes.get(i + 1) == Some(&b'/') {
            while i < bytes.len() && bytes[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        if matches!(bytes[i], b'{' | b'}') {
            out.push((bytes[i] as char).to_string());
            i += 1;
            continue;
        }
        if bytes[i] != b'"' {
            anyhow::bail!("invalid VDF token at byte {i}")
        }
        i += 1;
        let mut value = String::new();
        while i < bytes.len() && bytes[i] != b'"' {
            if bytes[i] == b'\\' && i + 1 < bytes.len() && matches!(bytes[i + 1], b'\\' | b'"') {
                i += 1;
            }
            value.push(bytes[i] as char);
            i += 1;
        }
        if i == bytes.len() {
            anyhow::bail!("unterminated VDF string")
        }
        i += 1;
        out.push(value);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses_nested_modern_vdf_and_escapes() {
        let text =
            r#""libraryfolders" { "0" { "path" "C:\\Program Files\\Steam" "apps" { "1" "2" } } }"#;
        let t = tokens(text).unwrap();
        assert!(t
            .windows(2)
            .any(|p| p == ["path", "C:\\Program Files\\Steam"]));
    }

    #[test]
    fn app_id_requires_matching_install_dir() {
        let text = r#""AppState" { "appid" "4125930" "name" "Punishing: Gray Raven" "installdir" "Punishing Gray Raven" "UserConfig" { "language" "english" } }"#;
        assert_eq!(
            manifest_app_id(text, "punishing gray raven").as_deref(),
            Some("4125930")
        );
        assert_eq!(manifest_app_id(text, "Other Game"), None);
        let bad = r#""AppState" { "appid" "41;x" "installdir" "Punishing Gray Raven" }"#;
        assert_eq!(manifest_app_id(bad, "Punishing Gray Raven"), None);
    }

    #[test]
    fn app_id_reads_owning_library() {
        let root = std::env::temp_dir().join(format!("ascnet-steam-{}", std::process::id()));
        let game = root.join("steamapps/common/Punishing Gray Raven");
        fs::create_dir_all(&game).unwrap();
        fs::write(
            root.join("steamapps/appmanifest_4125930.acf"),
            r#""AppState" { "appid" "4125930" "installdir" "Punishing Gray Raven" }"#,
        )
        .unwrap();
        fs::write(
            root.join("steamapps/appmanifest_1.acf"),
            r#""AppState" { "appid" "1" "installdir" "Else" }"#,
        )
        .unwrap();
        assert_eq!(app_id(&game).unwrap().as_deref(), Some("4125930"));
        assert_eq!(app_id(&root).unwrap(), None);
        fs::remove_dir_all(&root).unwrap();
    }
}
