#!/usr/bin/env python3
"""Rebuild a PGR-native-research style tree from an installed PGR client.

The client index is the source of truth for logical resource paths.  This
script only reads the installed client; it never modifies it.  By default it
extracts the Lua and BinaryTable bundles and writes decoded tables as both
JSON and TSV.  Referenced texture export is opt-in because the complete
client matrix is roughly 77 GB.

Requires UnityPy and msgpack.  The table decoder is shared with
decode_store_table.py and deliberately rejects formats it cannot prove how to
decode.
"""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import io
import json
import re
import struct
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import msgpack
import UnityPy

try:
    from decode_store_table import decode
except ModuleNotFoundError:  # Support importing Scripts.export_native_research in tests.
    from .decode_store_table import decode


REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO.parent / "PGR-native-research"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Punishing Gray Raven")
DEFAULT_KEY = "587865636f6472506547616b61326536"
TEXTURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tga", ".webp"}
ASSET_REFERENCE_RE = re.compile(
    r"(?i)assets/[a-z0-9_./-]+\.(?:png|jpg|jpeg|tga|webp)(?:\.ab)?"
)


@dataclass(frozen=True)
class BundleEntry:
    logical_path: str
    filename: str
    index_hash: str | None
    index_size: int | None
    source: Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_part(value: str) -> str:
    value = value.replace("\\", "/")
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip(" .")
    return value or "_"


def safe_relative(value: str) -> PurePosixPath:
    parts = [safe_part(part) for part in value.replace("\\", "/").split("/")]
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe relative path: {value!r}")
    return PurePosixPath(*parts)


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_json_rows(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    """Write table rows without materialising a second list and JSON string."""
    names: list[str] = []
    counts: defaultdict[str, int] = defaultdict(int)
    for header in headers:
        counts[header] += 1
        names.append(header if counts[header] == 1 else f"{header}#{counts[header]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("[\n")
        for index, row in enumerate(rows):
            if index:
                stream.write(",\n")
            json.dump(dict(zip(names, row, strict=True)), stream, ensure_ascii=False, indent=2)
        stream.write("\n]\n")


def normalise_asset_reference(value: str) -> str | None:
    value = value.replace("\\", "/").strip().lower()
    match = re.search(r"assets/[^\s\"']+", value)
    if match is None:
        return None
    value = match.group(0).rstrip(",);]}")
    if not value.startswith("assets/"):
        return None
    if not value.endswith(".ab"):
        value += ".ab"
    return value


def table_name(name: str) -> str:
    for suffix in (".tab.bytes", ".tab", ".bytes"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return safe_part(name)


def table_bundle_parts(logical_path: str) -> tuple[str, ...]:
    path = PurePosixPath(logical_path)
    parts = path.parts
    if len(parts) < 5 or parts[:3] != ("assets", "temp", "bytes"):
        raise ValueError(f"not a table bundle path: {logical_path}")
    return tuple(safe_part(part) for part in parts[3:-1]) + (safe_part(path.stem),)


def lua_bundle_name(logical_path: str) -> str:
    path = PurePosixPath(logical_path)
    return safe_part(path.stem)


def find_index(base: Path) -> Path:
    candidates = [base / "document" / "matrix" / "index", base / "resource" / "matrix" / "index"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("could not find StreamingAssets/{document,resource}/matrix/index")


def find_bundle(base: Path, filename: str) -> Path:
    if Path(filename).name != filename:
        raise ValueError(f"index contains an unsafe bundle name: {filename!r}")
    candidates = [base / "document" / "matrix" / filename, base / "resource" / "matrix" / filename]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(filename)


def parse_index(index_path: Path, base: Path) -> tuple[bytes, list[BundleEntry], dict[str, list[BundleEntry]]]:
    raw = index_path.read_bytes()
    env = UnityPy.load(raw)
    assets = [obj.read() for obj in env.objects if obj.type.name == "TextAsset"]
    if len(assets) != 1:
        raise ValueError(f"expected one index TextAsset, found {len(assets)}")
    payload = assets[0].m_Script.encode("utf-8", "surrogateescape")
    decoded = msgpack.unpackb(payload, strict_map_key=False)
    if not isinstance(decoded, list) or not decoded or not isinstance(decoded[0], dict):
        raise ValueError("index payload is not the expected msgpack catalog")

    entries: list[BundleEntry] = []
    by_filename: dict[str, list[BundleEntry]] = defaultdict(list)
    for logical, value in decoded[0].items():
        if not isinstance(logical, str):
            continue
        if isinstance(value, (list, tuple)):
            filename = value[0]
            index_hash = value[1] if len(value) > 1 and isinstance(value[1], str) else None
            index_size = value[2] if len(value) > 2 and isinstance(value[2], int) else None
        else:
            filename, index_hash, index_size = value, None, None
        if not isinstance(filename, str):
            continue
        source = find_bundle(base, filename)
        entry = BundleEntry(logical, filename, index_hash, index_size, source)
        entries.append(entry)
        by_filename[filename].append(entry)
    return payload, entries, by_filename


def asset_records(env: Any) -> Iterable[Any]:
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        yield obj


def object_name(obj: Any) -> str:
    try:
        value = obj.read().m_Name
    except Exception:
        value = ""
    return value if isinstance(value, str) else ""


def add_artifact(artifacts: list[dict[str, Any]], output: Path, path: Path, **metadata: Any) -> None:
    relative = path.relative_to(output).as_posix()
    data = path.read_bytes()
    artifacts.append({
        "path": relative,
        "size": len(data),
        "sha256": sha256(data),
        **metadata,
    })


def unique_path(path: Path, path_id: int) -> Path:
    # Extraction is intentionally idempotent.  The caller has already
    # de-duplicated logical bundle/name pairs for the current run, so an
    # existing path normally belongs to a previous run and should be replaced.
    return path


def text_asset_bytes(asset: Any) -> bytes:
    value = asset.m_Script
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise TypeError(f"unsupported TextAsset payload type: {type(value).__name__}")
    return value.encode("utf-8", "surrogateescape")


def json_rows(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    names: list[str] = []
    counts: defaultdict[str, int] = defaultdict(int)
    for header in headers:
        counts[header] += 1
        names.append(header if counts[header] == 1 else f"{header}#{counts[header]}")
    return [dict(zip(names, row, strict=True)) for row in rows]


def write_tsv(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def collect_references(value: Any, references: set[str]) -> None:
    if isinstance(value, str):
        for match in ASSET_REFERENCE_RE.finditer(value):
            normalised = normalise_asset_reference(match.group(0))
            if normalised is not None:
                references.add(normalised)
    elif isinstance(value, dict):
        for item in value.values():
            collect_references(item, references)
    elif isinstance(value, (list, tuple)):
        for item in value:
            collect_references(item, references)


def collect_existing_table_references(root: Path) -> set[str]:
    """Recover texture references for a texture-only resume run."""
    references: set[str] = set()
    if not root.is_dir():
        return references
    for path in root.rglob("*.json"):
        with path.open("r", encoding="utf-8", errors="ignore") as stream:
            for line in stream:
                for match in ASSET_REFERENCE_RE.finditer(line):
                    normalised = normalise_asset_reference(match.group(0))
                    if normalised is not None:
                        references.add(normalised)
    return references


def export_lua_bundle(
    entry: BundleEntry,
    output: Path,
    artifacts: list[dict[str, Any]],
    seen_names: set[tuple[str, str]],
) -> int:
    env = UnityPy.load(str(entry.source))
    count = 0
    for obj in asset_records(env):
        name = object_name(obj)
        if not name:
            continue
        path = output / "PGR_DATA" / "en" / "lua" / lua_bundle_name(entry.logical_path) / safe_part(name)
        key = (path.as_posix().lower(), name)
        if key in seen_names:
            continue
        seen_names.add(key)
        path = unique_path(path, obj.path_id)
        write_bytes(path, text_asset_bytes(obj.read()))
        add_artifact(
            artifacts,
            output,
            path,
            category="lua",
            asset_type="TextAsset",
            asset_name=name,
            path_id=str(obj.path_id),
            logical_path=entry.logical_path,
            bundle=entry.source.as_posix(),
        )
        count += 1
    return count


def export_table_bundle(
    entry: BundleEntry,
    output: Path,
    artifacts: list[dict[str, Any]],
    references: set[str],
    seen_names: set[tuple[str, str]],
    write_raw: bool,
) -> tuple[int, int]:
    env = UnityPy.load(str(entry.source))
    decoded_count = raw_count = 0
    parts = table_bundle_parts(entry.logical_path)
    byte_root = output / "PGR_DATA" / "en" / "bytes" / Path(*parts)
    tsv_root = output / "PGR_DATA" / "en" / "table" / Path(*parts)
    raw_root = output / "PGR_DATA" / "en" / "raw" / "bytes" / Path(*parts)
    for obj in asset_records(env):
        name = object_name(obj)
        if not name:
            continue
        key = (entry.logical_path, name)
        if key in seen_names:
            continue
        seen_names.add(key)
        data = text_asset_bytes(obj.read())
        stem = table_name(name)
        try:
            headers, rows = decode(data)
        except (ValueError, IndexError, UnicodeDecodeError, struct.error) as exc:
            raw_path = raw_root / f"{stem}.bin"
            write_bytes(raw_path, data)
            add_artifact(
                artifacts,
                output,
                raw_path,
                category="raw-table",
                asset_type="TextAsset",
                asset_name=name,
                path_id=str(obj.path_id),
                logical_path=entry.logical_path,
                bundle=entry.source.as_posix(),
                decode_error=str(exc),
            )
            raw_count += 1
            continue

        json_path = unique_path(byte_root / f"{stem}.json", obj.path_id)
        tsv_path = unique_path(tsv_root / f"{stem}.tsv", obj.path_id)
        write_json_rows(json_path, headers, rows)
        write_tsv(tsv_path, headers, rows)
        if write_raw:
            raw_path = unique_path(raw_root / f"{stem}.tab", obj.path_id)
            write_bytes(raw_path, data)
            add_artifact(
                artifacts,
                output,
                raw_path,
                category="raw-table",
                asset_type="TextAsset",
                asset_name=name,
                path_id=str(obj.path_id),
                logical_path=entry.logical_path,
                bundle=entry.source.as_posix(),
            )
        for item in (json_path, tsv_path):
            add_artifact(
                artifacts,
                output,
                item,
                category="table-json" if item.suffix == ".json" else "table-tsv",
                asset_type="TextAsset",
                asset_name=name,
                path_id=str(obj.path_id),
                logical_path=entry.logical_path,
                bundle=entry.source.as_posix(),
            )
        collect_references(rows, references)
        decoded_count += 1
    return decoded_count, raw_count


def texture_payload(obj: Any) -> bytes:
    asset = obj.read()
    image = asset.image
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def export_textures(
    entries: Iterable[BundleEntry],
    output: Path,
    artifacts: list[dict[str, Any]],
    requested: set[str],
    export_all: bool,
) -> tuple[int, int]:
    by_source: dict[Path, list[BundleEntry]] = defaultdict(list)
    for entry in entries:
        by_source[entry.source].append(entry)
    exported = failed = 0
    written_destinations: set[Path] = set()
    for source, source_entries in sorted(by_source.items(), key=lambda item: item[0].as_posix().lower()):
        logical_paths = {entry.logical_path for entry in source_entries}
        if not export_all and not logical_paths.intersection(requested):
            continue
        try:
            env = UnityPy.load(str(source))
        except Exception as exc:
            print(f"warning: could not load texture bundle {source}: {exc}", file=sys.stderr)
            failed += 1
            continue
        candidates = [
            entry.logical_path
            for entry in source_entries
            if export_all or entry.logical_path in requested
        ]
        names: dict[str, str] = {}
        for path in candidates:
            asset_name = PurePosixPath(path.removesuffix(".ab")).name.lower()
            names[asset_name] = path
            names[PurePosixPath(asset_name).stem] = path
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            name = object_name(obj)
            logical = names.get(name.lower())
            if logical is None:
                logical = next(
                    (
                        path
                        for path in candidates
                        if PurePosixPath(path.removesuffix(".ab")).stem.lower() == name.lower()
                    ),
                    None,
                )
            if logical is None and not export_all:
                continue
            if logical is None:
                logical = f"assets/_objects/{source.stem}/{safe_part(name or str(obj.path_id))}.png"
            destination = output / "PGR_DATA" / "en" / safe_relative(logical.removesuffix(".ab"))
            destination = destination.with_suffix(".png")
            if destination in written_destinations:
                destination = destination.with_name(f"{destination.stem}~{obj.path_id}{destination.suffix}")
            written_destinations.add(destination)
            try:
                write_bytes(destination, texture_payload(obj))
            except Exception as exc:
                print(f"warning: could not export texture {source}:{obj.path_id}: {exc}", file=sys.stderr)
                failed += 1
                continue
            add_artifact(
                artifacts,
                output,
                destination,
                category="texture",
                asset_type="Texture2D",
                asset_name=name,
                path_id=str(obj.path_id),
                logical_path=logical,
                bundle=source.as_posix(),
            )
            exported += 1
    return exported, failed


def write_sums(output: Path, artifacts: list[dict[str, Any]]) -> None:
    lines = [f"{item['sha256']}  {item['path']}" for item in sorted(artifacts, key=lambda item: item["path"])]
    (output / "PGR_DATA" / "en" / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    game = args.game_dir.resolve()
    base = game / "PGR_Data" / "StreamingAssets"
    index_path = find_index(base)
    UnityPy.set_assetbundle_decrypt_key(bytes.fromhex(args.key))
    index_payload, entries, by_filename = parse_index(index_path, base)
    print(f"Loaded {len(entries)} indexed resources from {index_path}", flush=True)

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    previous_manifest_path = output / "PGR_DATA" / "en" / "manifest.json"
    previous_manifest: dict[str, Any] = {}
    if previous_manifest_path.is_file():
        try:
            loaded = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous_manifest = loaded
        except (OSError, json.JSONDecodeError):
            previous_manifest = {}
    previous_artifacts = previous_manifest.get("artifacts", [])
    if not isinstance(previous_artifacts, list):
        previous_artifacts = []
    previous_stats = previous_manifest.get("stats", {})
    if not isinstance(previous_stats, dict):
        previous_stats = {}
    artifacts: list[dict[str, Any]] = []
    seen_lua: set[tuple[str, str]] = set()
    seen_tables: set[tuple[str, str]] = set()
    references: set[str] = set()
    stats: dict[str, Any] = {
        "index_entries": len(entries),
        "index_sha256": sha256(index_payload),
        "source_index": str(index_path),
        "source_matrix_directories": sorted({str(entry.source.parent) for entry in entries}),
        "bundles": len(by_filename),
        "lua_bundles": 0,
        "lua_text_assets": 0,
        "table_bundles": 0,
        "decoded_tables": 0,
        "unsupported_tables": 0,
        "texture_bundles": 0,
        "textures": 0,
        "texture_failures": 0,
    }

    if not args.no_lua:
        for entry in entries:
            if not entry.logical_path.startswith("assets/temp/lua/"):
                continue
            stats["lua_bundles"] += 1
            stats["lua_text_assets"] += export_lua_bundle(entry, output, artifacts, seen_lua)
        print(f"Exported {stats['lua_text_assets']} Lua TextAssets", flush=True)

    if not args.no_tables:
        for entry in entries:
            if not entry.logical_path.startswith("assets/temp/bytes/"):
                continue
            stats["table_bundles"] += 1
            decoded_count, raw_count = export_table_bundle(
                entry, output, artifacts, references, seen_tables, args.write_raw_tables
            )
            stats["decoded_tables"] += decoded_count
            stats["unsupported_tables"] += raw_count
            if stats["table_bundles"] % 10 == 0:
                gc.collect()
            if stats["table_bundles"] % 100 == 0:
                print(
                    f"Processed {stats['table_bundles']} table bundles; "
                    f"decoded {stats['decoded_tables']}",
                    flush=True,
                )
        print(
            f"Decoded {stats['decoded_tables']} tables; "
            f"preserved {stats['unsupported_tables']} unsupported tables",
            flush=True,
        )

    texture_entries = [
        entry
        for entry in entries
        if entry.logical_path.startswith("assets/product/texture/image/")
    ]
    if args.export_textures or args.all_textures:
        if args.no_tables and args.export_textures:
            reference_path = output / "PGR_DATA" / "en" / "texture_references.json"
            if reference_path.is_file():
                try:
                    loaded_references = json.loads(reference_path.read_text(encoding="utf-8"))
                    references.update(item for item in loaded_references if isinstance(item, str))
                except (OSError, json.JSONDecodeError):
                    pass
            if not references:
                references.update(collect_existing_table_references(output / "PGR_DATA" / "en" / "bytes"))
            print(f"Recovered {len(references)} texture references from existing JSON tables", flush=True)
            write_json(reference_path, sorted(references))
            add_artifact(artifacts, output, reference_path, category="metadata", asset_type="JSON")
        requested = references if args.export_textures else set()
        selected_entries = [entry for entry in texture_entries if args.all_textures or entry.logical_path in references]
        stats["texture_bundles"] = len({entry.source for entry in selected_entries})
        stats["textures"], stats["texture_failures"] = export_textures(
            texture_entries, output, artifacts, requested, args.all_textures
        )
        print(f"Exported {stats['textures']} textures", flush=True)

    if args.no_lua:
        for key in ("lua_bundles", "lua_text_assets"):
            if key in previous_stats:
                stats[key] = previous_stats[key]
    if args.no_tables:
        for key in ("table_bundles", "decoded_tables", "unsupported_tables", "referenced_texture_paths"):
            if key in previous_stats:
                stats[key] = previous_stats[key]
    if not (args.export_textures or args.all_textures):
        for key in ("texture_bundles", "textures", "texture_failures"):
            if key in previous_stats:
                stats[key] = previous_stats[key]

    data_root = output / "PGR_DATA" / "en"
    write_bytes(data_root / "index.msgpack", index_payload)
    write_json(data_root / "index.json", {
        "entries": [
            {
                "logical_path": entry.logical_path,
                "bundle": entry.filename,
                "index_hash": entry.index_hash,
                "index_size": entry.index_size,
                "source": entry.source.as_posix(),
            }
            for entry in entries
        ]
    })
    add_artifact(artifacts, output, data_root / "index.msgpack", category="index", asset_type="TextAsset")
    add_artifact(artifacts, output, data_root / "index.json", category="index", asset_type="JSON")
    if not args.no_tables:
        stats["referenced_texture_paths"] = len(references)

    version = {
        "game_version": args.version,
        "region": "en",
        "tool": "Scripts/export_native_research.py",
        "schema_version": 1,
        "index_sha256": stats["index_sha256"],
        "generated_from": str(game),
    }
    write_json(data_root / "version.json", version)
    add_artifact(artifacts, output, data_root / "version.json", category="metadata", asset_type="JSON")
    # Allow Lua, tables, and textures to be generated in separate runs without
    # losing the other phase's manifest entries.  Current artifacts win on a
    # path collision, which keeps reruns idempotent.
    merged_artifacts: dict[str, dict[str, Any]] = {
        item["path"]: item
        for item in previous_artifacts
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    merged_artifacts.update({item["path"]: item for item in artifacts})
    artifacts = list(merged_artifacts.values())
    stats["artifact_files"] = len(artifacts)
    stats["artifact_bytes"] = sum(item["size"] for item in artifacts)
    write_sums(output, artifacts)
    write_json(data_root / "manifest.json", {"version": version, "stats": stats, "artifacts": artifacts})
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, required=True, help="PGR installation directory")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output PGR-native-research directory")
    parser.add_argument("--version", default="4.7.0", help="client version label written to version.json")
    parser.add_argument("--key", default=DEFAULT_KEY, help="Unity AssetBundle decrypt key as hexadecimal")
    parser.add_argument("--no-lua", action="store_true", help="skip Lua TextAssets")
    parser.add_argument("--no-tables", action="store_true", help="skip BinaryTable bundles")
    parser.add_argument("--write-raw-tables", action="store_true", help="also preserve successfully decoded .tab bytes")
    parser.add_argument("--export-textures", action="store_true", help="export textures referenced by decoded tables")
    parser.add_argument("--all-textures", action="store_true", help="export every indexed product texture (large)")
    args = parser.parse_args()
    if len(args.key) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", args.key):
        parser.error("--key must be an even-length hexadecimal string")
    stats = run(args)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
