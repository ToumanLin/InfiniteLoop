#!/usr/bin/env python3
"""Extract one PGR Unity character prefab, meshes, skeleton, and animation graph.

This is intentionally a focused probe rather than a full-client exporter.  It
uses the exported PGR-native-research index to select one role and loads the
matching role bundles into one UnityPy environment so cross-bundle PPtrs can be
resolved.  The installed client is read-only.

The output contains OBJ meshes, a flat Transform hierarchy, Avatar TOS paths,
material/texture references, AnimatorController clip slots, state-to-clip
links, and AnimationClip curve summaries.  It does not try to synthesize an
FBX/GLB or emulate runtime scripts and IK.

Requires UnityPy and the 4.7.0 export's PGR_DATA/en/index.json.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import UnityPy


REPO = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = REPO.parent / "PGR-native-research-4.7.0" / "PGR_DATA" / "en" / "index.json"
DEFAULT_OUTPUT = REPO.parent / "PGR-native-research-4.7.0" / "PGR_DATA" / "en" / "model_samples"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Punishing Gray Raven")
DEFAULT_KEY = "587865636f6472506547616b61326536"


def safe_name(value: str, fallback: str = "_") -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip(" .")
    return value or fallback


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pptr_id(value: Any) -> int | None:
    path_id = getattr(value, "m_PathID", None)
    return path_id if isinstance(path_id, int) else None


def resolve(value: Any) -> Any | None:
    if not value or pptr_id(value) in (None, 0):
        return None
    try:
        return value.read()
    except (FileNotFoundError, KeyError, ValueError, IndexError, AttributeError):
        return None


def vector(value: Any, fields: tuple[str, ...]) -> list[float] | None:
    if value is None:
        return None
    result: list[float] = []
    for field in fields:
        item = getattr(value, field, None)
        if not isinstance(item, (int, float)):
            return None
        result.append(float(item))
    return result


def object_name(value: Any) -> str:
    name = getattr(value, "m_Name", "")
    return name if isinstance(name, str) else ""


def object_type(value: Any) -> str:
    return getattr(getattr(value, "type", None), "name", type(value).__name__)


def pptr_summary(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    target = resolve(value)
    return {
        "file_id": getattr(value, "m_FileID", None),
        "path_id": getattr(value, "m_PathID", None),
        "resolved_type": object_type(target) if target is not None else None,
        "resolved_name": object_name(target) if target is not None else None,
    }


def source_entry(entry: dict[str, Any], base: Path) -> tuple[dict[str, Any], Path] | None:
    filename = entry.get("bundle")
    if not isinstance(filename, str) or Path(filename).name != filename:
        return None
    for folder in ("document/matrix", "resource/matrix"):
        path = base / folder / filename
        if path.is_file():
            return entry, path
    return None


def load_entries(index_path: Path, game_dir: Path, model_key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError(f"index has no entries list: {index_path}")
    base = game_dir / "PGR_Data" / "StreamingAssets"
    model_key = model_key.lower()
    selected: list[dict[str, Any]] = []
    selected_paths: set[str] = set()
    prefab_entries: list[dict[str, Any]] = []
    for entry in entries:
        logical = entry.get("logical_path")
        if not isinstance(logical, str):
            continue
        lower = logical.lower()
        if not (
            lower.startswith("assets/product/role/")
            or lower.startswith("assets/pc/role/")
        ):
            continue
        if model_key not in lower:
            continue
        resolved_entry = source_entry(entry, base)
        if resolved_entry is None:
            continue
        if logical not in selected_paths:
            selected.append(entry)
            selected_paths.add(logical)
        if "/charaterprefab/" in lower and lower.endswith(".prefab.ab"):
            prefab_entries.append(entry)

    if not prefab_entries:
        raise FileNotFoundError(f"no role prefab matched --model {model_key!r}")
    prefab_entries.sort(key=lambda item: item["logical_path"])
    prefab = prefab_entries[0]
    if len(prefab_entries) > 1:
        print(
            f"warning: {len(prefab_entries)} role prefabs matched; using {prefab['logical_path']}",
            file=sys.stderr,
        )
    if len(selected) < 2:
        raise FileNotFoundError(f"matched prefab but no dependent role bundles for {model_key!r}")
    resolved = [source_entry(entry, base) for entry in selected]
    pairs = [item for item in resolved if item is not None]
    return pairs, {"entries": selected, "prefab": prefab, "game_dir": str(game_dir.resolve())}


def object_counts(objects: Iterable[Any]) -> dict[str, int]:
    counts: collections.Counter[str] = collections.Counter()
    for obj in objects:
        counts[obj.type.name] += 1
    return dict(sorted(counts.items()))


def component_type_map(prefab_objects: list[Any]) -> dict[int, tuple[str, str]]:
    result: dict[int, tuple[str, str]] = {}
    for obj in prefab_objects:
        if obj.type.name != "GameObject":
            continue
        game_object = obj.read()
        name = object_name(game_object)
        for pair in getattr(game_object, "m_Component", []):
            component = getattr(pair, "component", None)
            path_id = pptr_id(component)
            if path_id is not None:
                try:
                    result[path_id] = (name, component.type.name)
                except (AttributeError, ValueError):
                    result[path_id] = (name, "Unknown")
    return result


def transform_hierarchy(prefab_objects: list[Any]) -> tuple[list[dict[str, Any]], dict[int, str]]:
    transforms: dict[int, Any] = {}
    for obj in prefab_objects:
        if obj.type.name == "Transform":
            transforms[obj.path_id] = obj.read()

    nodes: list[dict[str, Any]] = []
    names: dict[int, str] = {}
    for path_id, transform in sorted(transforms.items(), key=lambda item: item[0]):
        game_object = resolve(transform.m_GameObject)
        name = object_name(game_object) or f"Transform_{path_id}"
        names[path_id] = name
        father = pptr_id(transform.m_Father) if transform.m_Father else None
        children = [pptr_id(child) for child in getattr(transform, "m_Children", [])]
        nodes.append(
            {
                "path_id": path_id,
                "name": name,
                "parent_path_id": father,
                "children_path_ids": [item for item in children if item is not None],
                "local_position": vector(getattr(transform, "m_LocalPosition", None), ("x", "y", "z")),
                "local_rotation": vector(
                    getattr(transform, "m_LocalRotation", None), ("x", "y", "z", "w")
                ),
                "local_scale": vector(getattr(transform, "m_LocalScale", None), ("x", "y", "z")),
            }
        )
    return nodes, names


def material_summary(renderer: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for pointer in getattr(renderer, "m_Materials", []):
        material = resolve(pointer)
        if material is None:
            result.append(pptr_summary(pointer) or {})
            continue
        textures: list[dict[str, Any]] = []
        properties = getattr(material, "m_SavedProperties", None)
        for property_name, tex_env in getattr(properties, "m_TexEnvs", []) if properties else []:
            texture_pointer = getattr(tex_env, "m_Texture", None)
            texture = resolve(texture_pointer)
            textures.append(
                {
                    "property": property_name,
                    "texture": object_name(texture) if texture is not None else None,
                    "texture_path_id": pptr_id(texture_pointer),
                }
            )
        result.append({"name": object_name(material), "path_id": getattr(pointer, "m_PathID", None), "textures": textures})
    return result


def renderer_summary(
    prefab_objects: list[Any], component_map: dict[int, tuple[str, str]], transform_names: dict[int, str]
) -> tuple[list[dict[str, Any]], dict[int, Any]]:
    renderers: list[dict[str, Any]] = []
    meshes: dict[int, Any] = {}
    for obj in prefab_objects:
        if obj.type.name != "SkinnedMeshRenderer":
            continue
        renderer = obj.read()
        mesh_pointer = getattr(renderer, "m_Mesh", None)
        mesh = resolve(mesh_pointer)
        mesh_id = pptr_id(mesh_pointer)
        if mesh is not None and mesh_id is not None:
            meshes[mesh_id] = mesh
        bones: list[dict[str, Any]] = []
        for bone_pointer in getattr(renderer, "m_Bones", []):
            bone_id = pptr_id(bone_pointer)
            bone = resolve(bone_pointer)
            bones.append(
                {
                    "path_id": bone_id,
                    "name": transform_names.get(bone_id or 0) or object_name(resolve(getattr(bone, "m_GameObject", None))),
                }
            )
        root_pointer = getattr(renderer, "m_RootBone", None)
        root_id = pptr_id(root_pointer)
        owner = component_map.get(obj.path_id, (None, None))
        renderers.append(
            {
                "path_id": obj.path_id,
                "game_object": owner[0],
                "mesh": object_name(mesh) if mesh is not None else None,
                "mesh_path_id": mesh_id,
                "root_bone": {"path_id": root_id, "name": transform_names.get(root_id or 0)},
                "bone_count": len(bones),
                "bones": bones,
                "materials": material_summary(renderer),
            }
        )
    return renderers, meshes


def avatar_summary(prefab_objects: list[Any]) -> dict[str, Any] | None:
    for obj in prefab_objects:
        if obj.type.name != "Animator":
            continue
        animator = obj.read()
        avatar_pointer = getattr(animator, "m_Avatar", None)
        avatar = resolve(avatar_pointer)
        if avatar is None:
            continue
        tos = [{"hash": item[0], "path": item[1]} for item in getattr(avatar, "m_TOS", [])]
        description = getattr(avatar, "m_HumanDescription", None)
        return {
            "name": object_name(avatar),
            "path_id": pptr_id(avatar_pointer),
            "tos_count": len(tos),
            "transform_paths": tos,
            "human_description": {
                "skeleton_count": len(getattr(description, "m_Skeleton", []) or []) if description else 0,
                "human_bone_count": len(getattr(description, "m_Human", []) or []) if description else 0,
                "root_motion_bone": getattr(description, "m_RootMotionBoneName", "") if description else "",
                "global_scale": getattr(description, "m_GlobalScale", None) if description else None,
            },
        }
    return None


def animator_summary(prefab_objects: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for obj in prefab_objects:
        if obj.type.name != "Animator":
            continue
        animator = obj.read()
        result.append(
            {
                "path_id": obj.path_id,
                "controller": pptr_summary(getattr(animator, "m_Controller", None)),
                "avatar": pptr_summary(getattr(animator, "m_Avatar", None)),
                "apply_root_motion": getattr(animator, "m_ApplyRootMotion", None),
                "update_mode": getattr(animator, "m_UpdateMode", None),
                "culling_mode": getattr(animator, "m_CullingMode", None),
            }
        )
    return result


def component_summary(
    prefab_objects: list[Any],
    component_map: dict[int, tuple[str, str]],
    transform_names: dict[int, str],
) -> list[dict[str, Any]]:
    """Record runtime component identities without executing their behaviour."""
    result: list[dict[str, Any]] = []
    for obj in prefab_objects:
        if obj.type.name != "MonoBehaviour":
            continue
        component = obj.read()
        owner, _ = component_map.get(obj.path_id, (None, None))
        script = resolve(getattr(component, "m_Script", None))
        script_name = object_name(script) if script is not None else None
        entry: dict[str, Any] = {
            "path_id": obj.path_id,
            "game_object": owner,
            "script": script_name,
            "enabled": getattr(component, "m_Enabled", None),
        }
        if script_name == "XDynamicBone":
            root_id = pptr_id(getattr(component, "Root", None))
            entry.update(
                {
                    "root": transform_names.get(root_id or 0),
                    "collider_count": len(getattr(component, "Colliders", []) or []),
                    "radius": getattr(component, "Radius", None),
                    "update_rate": getattr(component, "UpdateRate", None),
                    "time_correction_enabled": getattr(component, "TimeCorrectionEnabled", None),
                }
            )
        elif script_name == "XDynamicBoneManager":
            entry.update(
                {
                    "dynamic_bone_count": len(getattr(component, "DynamicBones", []) or []),
                    "use_disable_distance": getattr(component, "UseDisableDistance", None),
                    "disable_distance": getattr(component, "DisableDistance", None),
                    "weight": getattr(component, "Weight", None),
                    "time_correction_enabled": getattr(component, "EnableTimeCorrection", None),
                }
            )
        elif script_name == "XDynamicBoneCollider":
            entry.update(
                {
                    "radius": getattr(component, "Radius", None),
                    "height": getattr(component, "Height", None),
                    "direction": getattr(component, "XDirection", None),
                }
            )
        result.append(entry)
    return result


def clip_summary(pointer: Any, tos: dict[int, str]) -> dict[str, Any]:
    clip = resolve(pointer)
    if clip is None:
        return {"path_id": pptr_id(pointer), "resolved": False}
    bindings = getattr(getattr(clip, "m_ClipBindingConstant", None), "genericBindings", []) or []
    binding_path_hashes: list[int] = []
    for binding in bindings:
        path_hash = getattr(binding, "path", None)
        if isinstance(path_hash, int):
            binding_path_hashes.append(path_hash)
    return {
        "path_id": pptr_id(pointer),
        "resolved": True,
        "name": object_name(clip),
        "sample_rate": getattr(clip, "m_SampleRate", None),
        "muscle_clip_size": getattr(clip, "m_MuscleClipSize", None),
        "curve_counts": {
            "generic_bindings": len(bindings),
            "float": len(getattr(clip, "m_FloatCurves", []) or []),
            "position": len(getattr(clip, "m_PositionCurves", []) or []),
            "rotation": len(getattr(clip, "m_RotationCurves", []) or []),
            "scale": len(getattr(clip, "m_ScaleCurves", []) or []),
            "euler": len(getattr(clip, "m_EulerCurves", []) or []),
            "object_reference": len(getattr(clip, "m_PPtrCurves", []) or []),
            "events": len(getattr(clip, "m_Events", []) or []),
        },
        "binding_path_hashes": sorted(set(binding_path_hashes)),
    }


def state_clip_ids(state: Any) -> list[int]:
    result: list[int] = []
    for blend_tree_pointer in getattr(state, "m_BlendTreeConstantArray", []) or []:
        blend_tree = getattr(blend_tree_pointer, "data", None)
        for node_pointer in getattr(blend_tree, "m_NodeArray", []) if blend_tree else []:
            node = getattr(node_pointer, "data", None)
            clip_id = getattr(node, "m_ClipID", None) if node else None
            if isinstance(clip_id, int) and clip_id != 0xFFFFFFFF:
                result.append(clip_id)
    return sorted(set(result))


def controller_summary(controllers: list[Any], avatar_tos: dict[int, str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for obj in controllers:
        controller = obj.read()
        tos = {item[0]: item[1] for item in getattr(controller, "m_TOS", [])}
        binding_tos = dict(avatar_tos)
        binding_tos.update(tos)
        clip_slots: list[dict[str, Any]] = []
        resolved_clips: dict[int, dict[str, Any]] = {}
        binding_paths: dict[int, str | None] = {}
        for index, pointer in enumerate(getattr(controller, "m_AnimationClips", []) or []):
            summary = clip_summary(pointer, binding_tos)
            summary["controller_clip_index"] = index
            clip_slots.append(summary)
            if summary.get("resolved"):
                resolved_clips[index] = summary
            for path_hash in summary.get("binding_path_hashes", []):
                binding_paths[path_hash] = binding_tos.get(path_hash)

        layers: list[dict[str, Any]] = []
        constants = getattr(controller, "m_Controller", None)
        state_machines = getattr(constants, "m_StateMachineArray", []) if constants else []
        values = getattr(getattr(constants, "m_Values", None), "data", None) if constants else None
        parameters = [
            {
                "id": getattr(value, "m_ID", None),
                "name": tos.get(getattr(value, "m_ID", None)),
                "type": getattr(value, "m_Type", None),
                "index": getattr(value, "m_Index", None),
            }
            for value in getattr(values, "m_ValueArray", []) if values
        ]
        for layer_index, layer_pointer in enumerate(getattr(constants, "m_LayerArray", []) if constants else []):
            layer = getattr(layer_pointer, "data", None)
            state_machine_index = getattr(layer, "m_StateMachineIndex", None) if layer else None
            state_machine = None
            if isinstance(state_machine_index, int) and state_machine_index < len(state_machines):
                state_machine = getattr(state_machines[state_machine_index], "data", None)
            states: list[dict[str, Any]] = []
            for state_index, state_pointer in enumerate(getattr(state_machine, "m_StateConstantArray", []) if state_machine else []):
                state = getattr(state_pointer, "data", None)
                if state is None:
                    continue
                clip_ids = state_clip_ids(state)
                states.append(
                    {
                        "state_index": state_index,
                        "name_hash": getattr(state, "m_NameID", None),
                        "name": tos.get(getattr(state, "m_NameID", None)),
                        "full_path_hash": getattr(state, "m_FullPathID", None),
                        "full_path": tos.get(getattr(state, "m_FullPathID", None)),
                        "loop": getattr(state, "m_Loop", None),
                        "speed": getattr(state, "m_Speed", None),
                        "speed_parameter": tos.get(getattr(state, "m_SpeedParamID", None)),
                        "time_parameter": tos.get(getattr(state, "m_TimeParamID", None)),
                        "cycle_offset_parameter": tos.get(getattr(state, "m_CycleOffsetParamID", None)),
                        "mirror_parameter": tos.get(getattr(state, "m_MirrorParamID", None)),
                        "clip_indices": clip_ids,
                        "clips": [
                            {
                                "controller_clip_index": index,
                                "name": resolved_clips.get(index, {}).get("name"),
                                "resolved": index in resolved_clips,
                            }
                            for index in clip_ids
                        ],
                        "transition_count": len(getattr(state, "m_TransitionConstantArray", []) or []),
                    }
                )
            layers.append(
                {
                    "layer_index": layer_index,
                    "state_machine_index": state_machine_index,
                    "default_state_index": getattr(state_machine, "m_DefaultState", None) if state_machine else None,
                    "state_count": len(states),
                    "states": states,
                }
            )
        result.append(
            {
                "path_id": obj.path_id,
                "name": object_name(controller),
                "clip_slot_count": len(clip_slots),
                "resolved_clip_count": sum(1 for item in clip_slots if item.get("resolved")),
                "unresolved_clip_count": sum(1 for item in clip_slots if not item.get("resolved")),
                "layer_count": len(layers),
                "parameters": parameters,
                "binding_paths": [
                    {"hash": path_hash, "path": path}
                    for path_hash, path in sorted(binding_paths.items())
                ],
                "layers": layers,
                "clip_slots": clip_slots,
            }
        )
    return result


def export_meshes(meshes: dict[int, Any], output: Path) -> list[dict[str, Any]]:
    mesh_root = output / "meshes"
    result: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for path_id, mesh in sorted(meshes.items(), key=lambda item: (object_name(item[1]).lower(), item[0])):
        base_name = safe_name(object_name(mesh), f"mesh_{path_id}")
        name = base_name
        suffix = 2
        while name.lower() in used_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(name.lower())
        path = mesh_root / f"{name}.obj"
        entry: dict[str, Any] = {"name": object_name(mesh), "path_id": path_id, "obj": path.name}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(mesh.export("obj"), encoding="utf-8", newline="\n")
            entry["size"] = path.stat().st_size
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        result.append(entry)
    return result


def export_textures(objects: list[Any], output: Path) -> list[dict[str, Any]]:
    texture_root = output / "textures"
    result: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for obj in objects:
        if obj.type.name != "Texture2D":
            continue
        texture = obj.read()
        base_name = safe_name(object_name(texture), f"texture_{obj.path_id}")
        name = base_name
        suffix = 2
        while name.lower() in used_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(name.lower())
        path = texture_root / f"{name}.png"
        entry: dict[str, Any] = {"name": object_name(texture), "path_id": obj.path_id, "png": path.name}
        try:
            image = texture.image
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path, format="PNG")
            entry["size"] = path.stat().st_size
            entry["width"] = getattr(texture, "m_Width", None)
            entry["height"] = getattr(texture, "m_Height", None)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        result.append(entry)
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    index_path = args.index.resolve()
    game_dir = args.game_dir.resolve()
    pairs, selection = load_entries(index_path, game_dir, args.model)
    UnityPy.set_assetbundle_decrypt_key(bytes.fromhex(args.key))
    paths = list(dict.fromkeys(str(path) for _, path in pairs))
    print(f"Loading {len(paths)} role bundles for {args.model}", flush=True)
    env = UnityPy.Environment(*paths)
    prefab_path = next(path for entry, path in pairs if entry["logical_path"] == selection["prefab"]["logical_path"])
    prefab_bundle = env.files[str(prefab_path)]
    prefab_file = next(iter(prefab_bundle.files.values()))
    prefab_objects = [obj for obj in env.objects if obj.assets_file is prefab_file]
    component_map = component_type_map(prefab_objects)
    hierarchy, transform_names = transform_hierarchy(prefab_objects)
    renderers, meshes = renderer_summary(prefab_objects, component_map, transform_names)
    avatar = avatar_summary(prefab_objects)
    animators = animator_summary(prefab_objects)
    components = component_summary(prefab_objects, component_map, transform_names)
    model_key = args.model.lower()
    controller_prefix = args.controller_prefix.lower() if args.controller_prefix else model_key
    controllers = []
    for obj in env.objects:
        if obj.type.name != "AnimatorController":
            continue
        name = object_name(obj.read()).lower()
        if args.controller_prefix:
            matches = (
                name == controller_prefix
                or name.startswith(controller_prefix + "level")
                or name.startswith(controller_prefix + "display")
            )
        else:
            matches = name.startswith(controller_prefix)
        if matches:
            controllers.append(obj)
    sample_output = args.output.resolve() / safe_name(args.model)
    sample_output.mkdir(parents=True, exist_ok=True)
    mesh_files = export_meshes(meshes, sample_output)
    texture_files = export_textures(
        [obj for obj in env.objects if obj.assets_file in {item.assets_file for item in env.objects if item.assets_file}],
        sample_output,
    )
    report = {
        "schema_version": 1,
        "model_key": args.model,
        "controller_prefix": args.controller_prefix,
        "prefab": selection["prefab"]["logical_path"],
        "source": selection["game_dir"],
        "index": str(index_path),
        "selected_bundles": [
            {
                "logical_path": entry["logical_path"],
                "bundle": entry.get("bundle"),
                "index_size": entry.get("index_size"),
                "source": str(path),
            }
            for entry, path in pairs
        ],
        "loaded_object_counts": object_counts(env.objects),
        "prefab_object_counts": object_counts(prefab_objects),
        "hierarchy": {"root_count": sum(1 for node in hierarchy if node["parent_path_id"] is None), "nodes": hierarchy},
        "animators": animators,
        "components": components,
        "avatar": avatar,
        "skinned_mesh_renderers": renderers,
        "meshes": mesh_files,
        "textures": texture_files,
        "animator_controllers": controller_summary(
            controllers,
            {
                item["hash"]: item["path"]
                for item in (avatar or {}).get("transform_paths", [])
                if isinstance(item.get("hash"), int) and isinstance(item.get("path"), str)
            },
        ),
        "notes": [
            "The prefab Animator controller may be empty because the game assigns the controller at runtime.",
            "Controller state clip_indices refer to entries in that controller's clip_slots array.",
            "OBJ files contain geometry; skeleton and animation binding data are preserved in this JSON report.",
            "Runtime Lua/MonoBehaviour IK and gameplay animation events are not executed by this extractor.",
        ],
    }
    json_write(sample_output / "sample.json", report)
    print(json.dumps({
        "output": str(sample_output),
        "prefab": selection["prefab"]["logical_path"],
        "bundles": len(paths),
        "prefab_renderers": len(renderers),
        "runtime_components": len(components),
        "referenced_meshes": len(meshes),
        "controllers": len(controllers),
        "animation_clips": sum(item["resolved_clip_count"] for item in report["animator_controllers"]),
        "textures": sum(1 for item in texture_files if "error" not in item),
    }, ensure_ascii=False, indent=2), flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="r3dante", help="case-insensitive role key used in role bundle paths")
    parser.add_argument(
        "--controller-prefix",
        help="optional exact controller family prefix; includes the base, Level*, and Display controllers",
    )
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME, help="PGR installation directory")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX, help="exported PGR index.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="model_samples output directory")
    parser.add_argument("--key", default=DEFAULT_KEY, help="Unity AssetBundle decrypt key as hexadecimal")
    args = parser.parse_args()
    if len(args.key) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", args.key):
        parser.error("--key must be an even-length hexadecimal string")
    run(args)


if __name__ == "__main__":
    main()
