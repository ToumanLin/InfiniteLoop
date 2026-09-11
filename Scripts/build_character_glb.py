#!/usr/bin/env python3
"""Build a viewable glTF binary (GLB) for one extracted PGR Unity character.

The existing character probe writes OBJ/PNG/report files.  OBJ cannot carry
skinning, so this builder reopens the same read-only Unity bundles and uses
UnityPy's decoded MeshHandler data for the vertex attributes and bind poses.
The result contains the prefab Transform hierarchy, real SkinnedMeshRenderer
weights, inverse bind matrices, and the material textures used by the meshes.
Portable materials use unlit base color and source alpha/culling settings;
the game's custom lighting, packed normal maps, and shader effects are not
reproduced by glTF's standard material model.

AnimationController state/clip metadata remains in sample.json and is linked
from the GLB extras. Humanoid MuscleClip data is decoded into standard glTF
translation/rotation/scale animation channels; use --all-clips to include the
whole selected controller family.  For main-screen idle and interaction
motions, pass the character's ``*Display`` controller prefix (for example
``R5KalieninaMd010011Display``).

Requires UnityPy, Pillow (provided by UnityPy), and the 4.7.0 export index.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import re
import struct
import sys
from pathlib import Path
from typing import Any

import UnityPy
from UnityPy.helpers.MeshHelper import MeshHandler

from extract_unity_character import (
    DEFAULT_INDEX,
    DEFAULT_KEY,
    DEFAULT_GAME,
    load_entries,
    object_name,
    pptr_id,
    resolve,
    safe_name,
)


GLTF_COMPONENT_UNSIGNED_BYTE = 5121
GLTF_COMPONENT_UNSIGNED_SHORT = 5123
GLTF_COMPONENT_UNSIGNED_INT = 5125
GLTF_COMPONENT_FLOAT = 5126
GLTF_MODE_TRIANGLES = 4
GLTF_TARGET_ARRAY_BUFFER = 34962
GLTF_TARGET_ELEMENT_ARRAY_BUFFER = 34963


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def as_vec(value: Any, fields: tuple[str, ...], default: tuple[float, ...]) -> list[float]:
    if value is None:
        return list(default)
    result = []
    for field in fields:
        item = getattr(value, field, None)
        if not isinstance(item, (int, float)):
            return list(default)
        result.append(float(item))
    return result


def align4(data: bytearray) -> None:
    while len(data) % 4:
        data.append(0)


class BinaryBuilder:
    def __init__(self) -> None:
        self.data = bytearray()

    def add(self, payload: bytes, alignment: int = 4) -> tuple[int, int]:
        while len(self.data) % alignment:
            self.data.append(0)
        offset = len(self.data)
        self.data.extend(payload)
        return offset, len(payload)


class GltfBuilder:
    def __init__(self, model_name: str, extras: dict[str, Any]) -> None:
        self.bin = BinaryBuilder()
        self.gltf: dict[str, Any] = {
            "asset": {"version": "2.0", "generator": "PGR character GLB builder"},
            "scene": 0,
            "scenes": [{"nodes": []}],
            "nodes": [],
            "meshes": [],
            "skins": [],
            "materials": [],
            "textures": [],
            "images": [],
            "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
            "accessors": [],
            "bufferViews": [],
            "buffers": [],
            "animations": [],
            "extras": {"model": model_name, **extras},
        }
        self._texture_by_key: dict[tuple, int] = {}
        self._material_by_key: dict[tuple, int] = {}

    def accessor(
        self,
        payload: bytes,
        component_type: int,
        accessor_type: str,
        count: int,
        *,
        target: int | None = None,
        minimum: list[float] | None = None,
        maximum: list[float] | None = None,
        normalized: bool = False,
    ) -> int:
        dimensions = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
        sizes = {5121: 1, 5123: 2, 5125: 4, 5126: 4}
        expected = count * dimensions[accessor_type] * sizes[component_type]
        if count <= 0 or len(payload) != expected:
            raise ValueError(f"Invalid {accessor_type} accessor: {len(payload)} bytes, expected {expected}")
        offset, length = self.bin.add(payload)
        view: dict[str, Any] = {"buffer": 0, "byteOffset": offset, "byteLength": length}
        if target is not None:
            view["target"] = target
        view_index = len(self.gltf["bufferViews"])
        self.gltf["bufferViews"].append(view)
        accessor: dict[str, Any] = {
            "bufferView": view_index,
            "componentType": component_type,
            "count": count,
            "type": accessor_type,
        }
        if normalized:
            accessor["normalized"] = True
        if minimum is not None:
            accessor["min"] = minimum
        if maximum is not None:
            accessor["max"] = maximum
        accessor_index = len(self.gltf["accessors"])
        self.gltf["accessors"].append(accessor)
        return accessor_index

    def texture(self, texture: Any) -> int | None:
        if texture is None:
            return None
        key = asset_key(texture)
        if key in self._texture_by_key:
            return self._texture_by_key[key]
        try:
            image = texture.image
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            png = stream.getvalue()
        except Exception as exc:
            raise ValueError(f"Cannot decode texture {object_name(texture)}") from exc
        offset, length = self.bin.add(png)
        view_index = len(self.gltf["bufferViews"])
        self.gltf["bufferViews"].append(
            {"buffer": 0, "byteOffset": offset, "byteLength": length}
        )
        image_index = len(self.gltf["images"])
        self.gltf["images"].append(
            {
                "name": object_name(texture) or f"texture_{image_index}",
                "bufferView": view_index,
                "mimeType": "image/png",
            }
        )
        texture_index = len(self.gltf["textures"])
        self.gltf["textures"].append({"sampler": 0, "source": image_index})
        self._texture_by_key[key] = texture_index
        return texture_index

    def material(self, material: Any) -> int:
        if material is None:
            return self._fallback_material()
        key = asset_key(material)
        if key in self._material_by_key:
            return self._material_by_key[key]
        properties = getattr(material, "m_SavedProperties", None)
        tex_envs = {
            name: resolve(getattr(env, "m_Texture", None))
            for name, env in (getattr(properties, "m_TexEnvs", []) if properties else [])
        }
        floats = dict(getattr(properties, "m_Floats", []) or [])
        main_texture = tex_envs.get("_MainTex")
        if main_texture is None:
            for name in ("_BaseMap",):
                if tex_envs.get(name) is not None:
                    main_texture = tex_envs[name]
                    break
        pbr: dict[str, Any] = {
            "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
            "metallicFactor": 0.0,
            "roughnessFactor": 1.0,
        }
        main_texture_index = self.texture(main_texture)
        if main_texture_index is not None:
            pbr["baseColorTexture"] = {"index": main_texture_index}
        # Unity custom packed normal/flow maps are not glTF normal/emissive maps.
        # Preserve their names for shader reconstruction, not incorrect PBR slots.
        material_json: dict[str, Any] = {
            "name": object_name(material) or f"material_{len(self.gltf['materials'])}",
            "pbrMetallicRoughness": pbr,
            "doubleSided": floats.get("_Cull", 2.0) == 0.0,
            "extras": {
                "unity_shader": object_name(resolve(getattr(material, "m_Shader", None))) or None,
                "source_textures": sorted(
                    name for name, texture in tex_envs.items() if texture is not None
                ),
            },
        }
        if floats.get("_SrcBlend") == 5.0 and floats.get("_DstBlend") == 10.0:
            material_json["alphaMode"] = "BLEND"
        elif "_ALPHATEST_ON" in (getattr(material, "m_ShaderKeywords", "") or ""):
            material_json["alphaMode"] = "MASK"
            material_json["alphaCutoff"] = floats.get("_Cutoff", 0.5)
        pbr["baseColorFactor"][3] = max(0.0, min(1.0, floats.get("_Alpha", 1.0)))
        material_json["extras"]["unity_cull"] = floats.get("_Cull", 2.0)
        # The texture's RGB is the safest portable appearance for custom toon shaders.
        material_json["extensions"] = {"KHR_materials_unlit": {}}
        builder_extensions = self.gltf.setdefault("extensionsUsed", [])
        if "KHR_materials_unlit" not in builder_extensions:
            builder_extensions.append("KHR_materials_unlit")
        material_index = len(self.gltf["materials"])
        self.gltf["materials"].append(material_json)
        self._material_by_key[key] = material_index
        return material_index

    def _fallback_material(self) -> int:
        for index, material in enumerate(self.gltf["materials"]):
            if material.get("name") == "DefaultMaterial":
                return index
        index = len(self.gltf["materials"])
        self.gltf["materials"].append(
            {
                "name": "DefaultMaterial",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.7, 0.7, 0.7, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
                "doubleSided": True,
            }
        )
        return index

    def finish(self, path: Path) -> None:
        align4(self.bin.data)
        self.gltf["buffers"] = [{"byteLength": len(self.bin.data)}]
        json_bytes = json.dumps(self.gltf, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
        bin_bytes = bytes(self.bin.data)
        bin_bytes += b"\0" * ((4 - len(bin_bytes) % 4) % 4)
        total_length = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
        output = bytearray()
        output.extend(struct.pack("<4sII", b"glTF", 2, total_length))
        output.extend(struct.pack("<I4s", len(json_bytes), b"JSON"))
        output.extend(json_bytes)
        output.extend(struct.pack("<I4s", len(bin_bytes), b"BIN\0"))
        output.extend(bin_bytes)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(output)


def pack_floats(values: list[float] | list[tuple[float, ...]]) -> bytes:
    flat: list[float] = []
    for value in values:
        if isinstance(value, (tuple, list)):
            flat.extend(float(item) for item in value)
        else:
            flat.append(float(value))
    return struct.pack(f"<{len(flat)}f", *flat)


def pack_uints(values: list[int], component_type: int) -> bytes:
    char = {GLTF_COMPONENT_UNSIGNED_BYTE: "B", GLTF_COMPONENT_UNSIGNED_SHORT: "H", GLTF_COMPONENT_UNSIGNED_INT: "I"}[component_type]
    return struct.pack(f"<{len(values)}{char}", *values)


def minmax(values: list[tuple[float, ...]]) -> tuple[list[float], list[float]]:
    dimensions = len(values[0])
    return (
        [min(value[index] for value in values) for index in range(dimensions)],
        [max(value[index] for value in values) for index in range(dimensions)],
    )


def asset_key(asset: Any) -> tuple:
    """Path IDs are scoped to a serialized file; Python read() objects are temporary."""
    reader = asset.object_reader
    return (id(reader.assets_file), reader.path_id)


def unit_normal(value: tuple[float, ...]) -> tuple[float, ...]:
    length = math.sqrt(sum(v * v for v in value))
    if not math.isfinite(length) or length <= 0:
        raise ValueError("Invalid mesh normal")
    return tuple(v / length for v in value)


def skin_attributes(indices: Any, weights: Any, count: int, bone_count: int) -> tuple[list, list]:
    if not indices or len(indices) != count or (weights and len(weights) != count):
        raise ValueError("Skin attribute count differs from vertex count")
    joint_rows, weight_rows = [], []
    for vertex, row in enumerate(indices):
        joints = [int(j) for j in row]
        if not 1 <= len(joints) <= 4:
            raise ValueError(f"Unsupported skin influence count at vertex {vertex}")
        if not weights:
            # Unity omits the weight stream for rigid, one-influence meshes.
            if len(joints) != 1:
                raise ValueError("Missing weights on a non-rigid mesh")
            current = [1.0]
        else:
            current = [float(w) for w in weights[vertex]]
        if len(current) != len(joints) or any(not math.isfinite(w) or w < 0 for w in current):
            raise ValueError(f"Invalid weights at vertex {vertex}")
        if sum(current) <= 0:
            raise ValueError(f"Zero weight sum at vertex {vertex}")
        for i, (joint, weight) in enumerate(zip(joints, current)):
            if weight == 0:
                joints[i] = 0
            elif not 0 <= joint < bone_count:
                raise ValueError(f"Joint {joint} outside skin at vertex {vertex}")
        total = sum(current)
        joint_rows.append(tuple(joints + [0] * (4 - len(joints))))
        weight_rows.append(tuple([w / total for w in current] + [0.0] * (4 - len(current))))
    return joint_rows, weight_rows


def common_ancestor(nodes: list[dict], joints: list[int]) -> int:
    parents = {child: i for i, node in enumerate(nodes) for child in node.get("children", [])}
    def lineage(node: int) -> list[int]:
        result = [node]
        while node in parents:
            node = parents[node]
            if node in result:
                raise ValueError("Cyclic transform hierarchy")
            result.append(node)
        return result
    paths = [lineage(j) for j in joints]
    for ancestor in paths[0]:
        if all(ancestor in path for path in paths[1:]):
            return ancestor
    raise ValueError("Skin joints have no common ancestor")


def active_transform(path_id: int | None, transforms: dict[int, Any]) -> bool:
    while path_id in transforms:
        transform = transforms[path_id]
        game_object = resolve(transform.m_GameObject)
        if game_object is None:
            raise ValueError("Unresolved renderer GameObject")
        if not game_object.m_IsActive:
            return False
        path_id = pptr_id(transform.m_Father)
    return True


def matrix_values(matrix: Any) -> list[float]:
    """Serialize Unity's row-field Matrix4x4f as glTF column-major MAT4."""
    rows = [
        [float(getattr(matrix, f"e{row}{column}")) for column in range(4)]
        for row in range(4)
    ]
    # Change handedness consistently with vertices and node transforms: C M C.
    signs = (-1, 1, 1, 1)
    return [rows[row][column] * signs[row] * signs[column] for column in range(4) for row in range(4)]


def transform_maps(prefab_objects: list[Any]) -> tuple[dict[int, Any], dict[int, int], dict[int, int], dict[int, str]]:
    transforms: dict[int, Any] = {
        obj.path_id: obj.read() for obj in prefab_objects if obj.type.name == "Transform"
    }
    game_object_to_transform: dict[int, int] = {}
    component_to_transform: dict[int, int] = {}
    transform_names: dict[int, str] = {}
    for path_id, transform in transforms.items():
        game_object = resolve(getattr(transform, "m_GameObject", None))
        game_object_id = pptr_id(getattr(transform, "m_GameObject", None))
        if game_object_id is not None:
            game_object_to_transform[game_object_id] = path_id
        name = object_name(game_object) or f"Transform_{path_id}"
        transform_names[path_id] = name
        if game_object is not None:
            for component in getattr(game_object, "m_Component", []):
                pointer = getattr(component, "component", None)
                component_id = pptr_id(pointer)
                if component_id is not None:
                    component_to_transform[component_id] = path_id
    return transforms, game_object_to_transform, component_to_transform, transform_names


def hierarchy_paths(transforms: dict[int, Any], transform_names: dict[int, str]) -> dict[str, int]:
    """Return Unity Transform paths, including the prefab root prefix."""
    children: dict[int, list[int]] = {path_id: [] for path_id in transforms}
    for path_id, transform in transforms.items():
        parent_id = pptr_id(getattr(transform, "m_Father", None))
        if parent_id in children:
            children[parent_id].append(path_id)
    result: dict[str, int] = {}

    def visit(path_id: int, prefix: str) -> None:
        path = f"{prefix}/{transform_names[path_id]}" if prefix else transform_names[path_id]
        result[path] = path_id
        for child_id in children[path_id]:
            visit(child_id, path)

    for path_id in transforms:
        parent_id = pptr_id(getattr(transforms[path_id], "m_Father", None))
        if parent_id not in transforms:
            visit(path_id, "")
    return result


def animation_path_nodes(
    builder: GltfBuilder,
    transforms: dict[int, Any],
    transform_names: dict[int, str],
    node_by_transform: dict[int, int],
    avatar: Any | None,
) -> dict[str, int]:
    """Map both full prefab paths and Avatar TOS paths to glTF node indices."""
    paths = hierarchy_paths(transforms, transform_names)
    aliases: dict[str, int] = {}
    for path, path_id in paths.items():
        aliases[path] = node_by_transform[path_id]
        parts = path.split("/", 1)
        if len(parts) == 2:
            aliases[parts[1]] = node_by_transform[path_id]
    if avatar is not None:
        for avatar_hash, avatar_path in getattr(avatar, "m_TOS", []) or []:
            if not isinstance(avatar_path, str):
                continue
            node_index = aliases.get(avatar_path)
            for full_path, path_id in paths.items():
                if full_path == avatar_path or full_path.endswith("/" + avatar_path):
                    node_index = node_by_transform[path_id]
                    break
            if node_index is not None:
                aliases[avatar_path] = node_index
                aliases[str(avatar_hash)] = node_index
                aliases[avatar_hash] = node_index
    builder.gltf["extras"]["animation_path_count"] = len(aliases)
    return aliases


def binding_width(binding: Any) -> int:
    if getattr(binding, "typeID", None) == 4:
        if getattr(binding, "attribute", None) == 2:
            return 4
        if getattr(binding, "attribute", None) in (1, 3, 4):
            return 3
    return 1


def expanded_bindings(bindings: list[Any]) -> list[Any]:
    expanded: list[Any] = []
    for binding in bindings:
        expanded.extend([binding] * binding_width(binding))
    return expanded


def streamed_frames(streamed: Any) -> list[tuple[float, list[tuple[int, float]]]]:
    raw = struct.pack(f"<{len(streamed.data)}I", *streamed.data)
    position = 0
    frames: list[tuple[float, list[tuple[int, float]]]] = []
    while position + 8 <= len(raw):
        time, key_count = struct.unpack_from("<fI", raw, position)
        position += 8
        if key_count > 10000 or position + key_count * 20 > len(raw):
            raise ValueError("invalid Unity StreamedClip frame")
        keys: list[tuple[int, float]] = []
        for _ in range(key_count):
            index = struct.unpack_from("<I", raw, position)[0]
            value = struct.unpack_from("<4f", raw, position + 4)[3]
            position += 20
            keys.append((index, value))
        frames.append((time, keys))
    if position != len(raw):
        raise ValueError("trailing bytes in Unity StreamedClip")
    return frames


def normalize_quaternion(values: list[float]) -> list[float]:
    length = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(length) or length <= 0:
        return [0.0, 0.0, 0.0, 1.0]
    return [value / length for value in values]


def add_curve_value(
    tracks: dict[tuple[int, str], dict[float, list[float]]],
    bindings: list[Any],
    index: int,
    data: list[float],
    offset: int,
    cursor: int,
    time: float,
    path_nodes: dict[str, int],
) -> int:
    if not 0 <= index < len(bindings):
        return max(cursor + 1, cursor)
    binding = bindings[index]
    width = binding_width(binding)
    values = data[offset + cursor : offset + cursor + width]
    if len(values) != width:
        return cursor + width
    if getattr(binding, "typeID", None) != 4 or not math.isfinite(time):
        return cursor + width
    path = path_nodes.get(str(getattr(binding, "path", "")))
    if path is None:
        # The report stores Avatar TOS paths, while clips store the CRC hash.
        path = path_nodes.get(getattr(binding, "path", ""))
    if path is None:
        return cursor + width
    attribute = getattr(binding, "attribute", None)
    if attribute == 1 and width == 3:
        value = [-float(values[0]), float(values[1]), float(values[2])]
        target = "translation"
    elif attribute == 2 and width == 4:
        value = normalize_quaternion([float(values[0]), -float(values[1]), -float(values[2]), float(values[3])])
        target = "rotation"
    elif attribute == 3 and width == 3:
        value = [float(values[0]), float(values[1]), float(values[2])]
        target = "scale"
    elif attribute == 4 and width == 3:
        # Unity Euler bindings are uncommon in this character.  Keep them as
        # rotation values only when they are present; consumers can inspect
        # the raw channel in extras if a project needs a different order.
        value = [float(values[0]), -float(values[1]), -float(values[2])]
        target = "rotation"
    else:
        return cursor + width
    tracks.setdefault((path, target), {})[round(float(time), 6)] = value
    return cursor + width


def decode_animation_clip(clip: Any, path_nodes: dict[str, int]) -> dict[tuple[int, str], dict[float, list[float]]]:
    tracks: dict[tuple[int, str], dict[float, list[float]]] = {}
    direct_bindings = expanded_bindings(getattr(getattr(clip, "m_ClipBindingConstant", None), "genericBindings", []) or [])
    muscle = getattr(clip, "m_MuscleClip", None)
    clip_data = getattr(getattr(muscle, "m_Clip", None), "data", None) if muscle else None
    if clip_data is not None:
        streamed = getattr(clip_data, "m_StreamedClip", None)
        streamed_bindings = direct_bindings
        for time, keys in streamed_frames(streamed):
            if not math.isfinite(time) or not keys:
                continue
            cursor = 0
            for index, _ in keys:
                cursor = add_curve_value(
                    tracks, streamed_bindings, index, [item[1] for item in keys], 0, cursor, time, path_nodes
                )
        dense = getattr(clip_data, "m_DenseClip", None)
        stream_count = int(getattr(streamed, "curveCount", 0) or 0)
        dense_count = int(getattr(dense, "m_CurveCount", 0) or 0)
        dense_data = list(getattr(dense, "m_SampleArray", []) or [])
        dense_rate = float(getattr(dense, "m_SampleRate", 0.0) or 0.0)
        if dense_rate > 0:
            for frame_index in range(int(getattr(dense, "m_FrameCount", 0) or 0)):
                time = float(getattr(dense, "m_BeginTime", 0.0) or 0.0) + frame_index / dense_rate
                offset = frame_index * dense_count
                cursor = 0
                while cursor < dense_count:
                    cursor = add_curve_value(
                        tracks, direct_bindings, stream_count + cursor, dense_data, offset, cursor, time, path_nodes
                    )
        constant = getattr(clip_data, "m_ConstantClip", None)
        constant_data = list(getattr(constant, "data", []) or []) if constant else []
        if constant_data:
            base = stream_count + dense_count
            stop_time = float(getattr(muscle, "m_StopTime", 0.0) or 0.0)
            for time in (0.0, stop_time):
                cursor = 0
                while cursor < len(constant_data):
                    cursor = add_curve_value(
                        tracks, direct_bindings, base + cursor, constant_data, 0, cursor, time, path_nodes
                    )
    for curve in getattr(clip, "m_PositionCurves", []) or []:
        node = path_nodes.get(curve.path)
        if node is not None:
            for key in getattr(curve.curve, "m_Curve", []) or []:
                tracks.setdefault((node, "translation"), {})[round(float(key.time), 6)] = [
                    -float(key.value.x), float(key.value.y), float(key.value.z)
                ]
    for curve in getattr(clip, "m_RotationCurves", []) or []:
        node = path_nodes.get(curve.path)
        if node is not None:
            for key in getattr(curve.curve, "m_Curve", []) or []:
                tracks.setdefault((node, "rotation"), {})[round(float(key.time), 6)] = normalize_quaternion([
                    float(key.value.x), -float(key.value.y), -float(key.value.z), float(key.value.w)
                ])
    for curve in getattr(clip, "m_ScaleCurves", []) or []:
        node = path_nodes.get(curve.path)
        if node is not None:
            for key in getattr(curve.curve, "m_Curve", []) or []:
                tracks.setdefault((node, "scale"), {})[round(float(key.time), 6)] = [
                    float(key.value.x), float(key.value.y), float(key.value.z)
                ]
    return tracks


def add_animation(builder: GltfBuilder, clip: Any, path_nodes: dict[str, int], name: str) -> dict[str, Any]:
    tracks = decode_animation_clip(clip, path_nodes)
    samplers: list[dict[str, Any]] = []
    channels: list[dict[str, Any]] = []
    for (node, target), values_by_time in sorted(tracks.items()):
        if not values_by_time:
            continue
        times = sorted(values_by_time)
        values = [values_by_time[time] for time in times]
        input_accessor = builder.accessor(
            pack_floats(times), GLTF_COMPONENT_FLOAT, "SCALAR", len(times),
            minimum=[times[0]], maximum=[times[-1]],
        )
        value_type = "VEC4" if target == "rotation" else "VEC3"
        output_accessor = builder.accessor(pack_floats(values), GLTF_COMPONENT_FLOAT, value_type, len(values))
        sampler_index = len(samplers)
        samplers.append({"input": input_accessor, "output": output_accessor, "interpolation": "LINEAR"})
        channels.append({"sampler": sampler_index, "target": {"node": node, "path": target}})
    if not channels:
        raise ValueError(f"AnimationClip {object_name(clip)} produced no Transform channels")
    animation = {
        "name": name,
        "samplers": samplers,
        "channels": channels,
        "extras": {
            "unity_clip": object_name(clip),
            "sample_rate": getattr(clip, "m_SampleRate", None),
            "duration": getattr(getattr(clip, "m_MuscleClip", None), "m_StopTime", None),
            "channel_count": len(channels),
        },
    }
    builder.gltf["animations"].append(animation)
    return {"name": name, "channels": len(channels), "samplers": len(samplers)}


def matching_animation_clips(env: Any, controller_prefix: str | None) -> list[Any]:
    prefix = (controller_prefix or "").lower()
    controllers = []
    for obj in env.objects:
        if obj.type.name != "AnimatorController":
            continue
        name = object_name(obj.read()).lower()
        if not prefix or name == prefix or name.startswith(prefix + "level") or name.startswith(prefix + "display"):
            controllers.append(obj.read())
    result: list[Any] = []
    seen: set[tuple] = set()
    for controller in controllers:
        for pointer in getattr(controller, "m_AnimationClips", []) or []:
            clip = resolve(pointer)
            if clip is None:
                continue
            key = asset_key(clip)
            if key not in seen:
                seen.add(key)
                result.append(clip)
    return result


def add_transform_nodes(builder: GltfBuilder, transforms: dict[int, Any], transform_names: dict[int, str]) -> dict[int, int]:
    node_by_transform: dict[int, int] = {}
    for path_id in sorted(transforms):
        transform = transforms[path_id]
        node: dict[str, Any] = {
            "name": transform_names[path_id],
            "translation": as_vec(getattr(transform, "m_LocalPosition", None), ("x", "y", "z"), (0.0, 0.0, 0.0)),
            "rotation": as_vec(getattr(transform, "m_LocalRotation", None), ("x", "y", "z", "w"), (0.0, 0.0, 0.0, 1.0)),
            "scale": as_vec(getattr(transform, "m_LocalScale", None), ("x", "y", "z"), (1.0, 1.0, 1.0)),
            "extras": {"unity_transform_path_id": path_id},
        }
        node["translation"][0] *= -1
        node["rotation"][1] *= -1
        node["rotation"][2] *= -1
        length = math.sqrt(sum(v * v for v in node["rotation"]))
        if not length:
            raise ValueError(f"Zero quaternion on {transform_names[path_id]}")
        node["rotation"] = [v / length for v in node["rotation"]]
        node_by_transform[path_id] = len(builder.gltf["nodes"])
        builder.gltf["nodes"].append(node)
    for path_id, transform in transforms.items():
        node = builder.gltf["nodes"][node_by_transform[path_id]]
        child_ids = [pptr_id(child) for child in getattr(transform, "m_Children", [])]
        children = [node_by_transform[child_id] for child_id in child_ids if child_id in node_by_transform]
        if children:
            node["children"] = children
        parent_id = pptr_id(getattr(transform, "m_Father", None))
        if parent_id not in node_by_transform:
            builder.gltf["scenes"][0]["nodes"].append(node_by_transform[path_id])
    return node_by_transform


def add_skin(builder: GltfBuilder, renderer: Any, mesh: Any, node_by_transform: dict[int, int], transform_names: dict[int, str]) -> tuple[int, list[int]]:
    bone_pointers = getattr(renderer, "m_Bones", []) or []
    joint_ids = [pptr_id(pointer) for pointer in bone_pointers]
    joints = [node_by_transform[bone_id] for bone_id in joint_ids if bone_id in node_by_transform]
    if len(joints) != len(joint_ids):
        missing = [bone_id for bone_id in joint_ids if bone_id not in node_by_transform]
        raise ValueError(f"renderer {object_name(renderer)} has missing prefab bone transforms: {missing[:4]}")
    bindposes = getattr(mesh, "m_BindPose", None) or []
    if len(bindposes) != len(joints):
        raise ValueError(
            f"mesh {object_name(mesh)} has {len(bindposes)} bind poses for {len(joints)} renderer bones"
        )
    payload = pack_floats([matrix_values(matrix) for matrix in bindposes])
    inverse_bind_accessor = builder.accessor(payload, GLTF_COMPONENT_FLOAT, "MAT4", len(bindposes))
    skin: dict[str, Any] = {
        "name": f"{object_name(renderer)}_Skin",
        "joints": joints,
        "inverseBindMatrices": inverse_bind_accessor,
        "extras": {"unity_bones": [transform_names.get(path_id, str(path_id)) for path_id in joint_ids]},
    }
    if not joints:
        raise ValueError(f"No joints for {object_name(mesh)}")
    skin["skeleton"] = common_ancestor(builder.gltf["nodes"], joints)
    skin_index = len(builder.gltf["skins"])
    builder.gltf["skins"].append(skin)
    return skin_index, joint_ids


def add_renderer(builder: GltfBuilder, renderer: Any, mesh: Any, owner_transform_id: int | None, node_by_transform: dict[int, int], transform_names: dict[int, str]) -> dict[str, Any]:
    handler = MeshHandler(mesh)
    handler.process()
    vertices = [(-float(v[0]), float(v[1]), float(v[2])) for v in (handler.m_Vertices or [])]
    if not vertices or not handler.m_IndexBuffer:
        raise ValueError(f"mesh {object_name(mesh)} has no decoded geometry")
    minimum, maximum = minmax(vertices)
    position_accessor = builder.accessor(
        pack_floats(vertices), GLTF_COMPONENT_FLOAT, "VEC3", len(vertices),
        target=GLTF_TARGET_ARRAY_BUFFER, minimum=minimum, maximum=maximum,
    )
    normal_accessor = None
    if handler.m_Normals and len(handler.m_Normals) == len(vertices):
        normals = [unit_normal((-float(n[0]), float(n[1]), float(n[2]))) for n in handler.m_Normals]
        normal_accessor = builder.accessor(pack_floats(normals), GLTF_COMPONENT_FLOAT, "VEC3", len(normals), target=GLTF_TARGET_ARRAY_BUFFER)
    uv_accessor = None
    if handler.m_UV0 and len(handler.m_UV0) == len(vertices):
        uvs = [(float(uv[0]), 1.0 - float(uv[1])) for uv in handler.m_UV0]
        uv_accessor = builder.accessor(pack_floats(uvs), GLTF_COMPONENT_FLOAT, "VEC2", len(uvs), target=GLTF_TARGET_ARRAY_BUFFER)
    joints, weights = skin_attributes(handler.m_BoneIndices, handler.m_BoneWeights,
                                      len(vertices), len(renderer.m_Bones))
    joints_accessor = builder.accessor(pack_uints([item for row in joints for item in row], GLTF_COMPONENT_UNSIGNED_SHORT), GLTF_COMPONENT_UNSIGNED_SHORT, "VEC4", len(joints), target=GLTF_TARGET_ARRAY_BUFFER)
    weights_accessor = builder.accessor(pack_floats(weights), GLTF_COMPONENT_FLOAT, "VEC4", len(weights), target=GLTF_TARGET_ARRAY_BUFFER)
    renderer_name = transform_names.get(owner_transform_id or 0) or object_name(renderer) or object_name(mesh)
    skin_index, joint_ids = add_skin(builder, renderer, mesh, node_by_transform, transform_names)
    attributes: dict[str, int] = {"POSITION": position_accessor}
    if normal_accessor is not None:
        attributes["NORMAL"] = normal_accessor
    if uv_accessor is not None:
        attributes["TEXCOORD_0"] = uv_accessor
    if joints_accessor is not None and weights_accessor is not None:
        attributes["JOINTS_0"] = joints_accessor
        attributes["WEIGHTS_0"] = weights_accessor
    material_pointers = getattr(renderer, "m_Materials", []) or []
    materials = [resolve(pointer) for pointer in material_pointers]
    primitives: list[dict[str, Any]] = []
    triangles_by_submesh = handler.get_triangles()
    for submesh_index, triangles in enumerate(triangles_by_submesh):
        material = materials[submesh_index] if submesh_index < len(materials) else None
        props = getattr(material, "m_SavedProperties", None)
        cull = dict(getattr(props, "m_Floats", []) or []).get("_Cull", 2.0)
        # Reflect winding for handedness; front-cull passes need the opposite side.
        base_vertex = getattr(mesh.m_SubMeshes[submesh_index], "baseVertex", 0) or 0
        indices = [int(i) + base_vertex for tri in triangles
                   for i in (tri if cull == 1.0 else (tri[0], tri[2], tri[1]))]
        if indices and (min(indices) < 0 or max(indices) >= len(vertices)):
            raise ValueError(f"Out-of-range triangle indices in {object_name(mesh)}")
        if not indices:
            continue
        max_index = max(indices)
        component_type = GLTF_COMPONENT_UNSIGNED_SHORT if max_index <= 65535 else GLTF_COMPONENT_UNSIGNED_INT
        index_accessor = builder.accessor(pack_uints(indices, component_type), component_type, "SCALAR", len(indices), target=GLTF_TARGET_ELEMENT_ARRAY_BUFFER, minimum=[min(indices)], maximum=[max(indices)])
        primitive: dict[str, Any] = {
            "attributes": attributes.copy(),
            "indices": index_accessor,
            "mode": GLTF_MODE_TRIANGLES,
            "material": builder.material(materials[submesh_index]) if submesh_index < len(materials) else builder._fallback_material(),
        }
        primitives.append(primitive)
    mesh_index = len(builder.gltf["meshes"])
    builder.gltf["meshes"].append({"name": object_name(mesh) or f"mesh_{mesh_index}", "primitives": primitives})
    mesh_node: dict[str, Any] = {
        "name": f"{renderer_name}__mesh",
        "mesh": mesh_index,
        "skin": skin_index,
        "extras": {"unity_renderer": renderer_name, "bone_count": len(joint_ids)},
    }
    mesh_node_index = len(builder.gltf["nodes"])
    builder.gltf["nodes"].append(mesh_node)
    # glTF skinned positions are already placed by jointWorld * inverseBind.
    # Keep the mesh node at scene identity; owner transforms remain in the rig.
    builder.gltf["scenes"][0]["nodes"].append(mesh_node_index)
    return {
        "renderer": renderer_name,
        "mesh": object_name(mesh),
        "vertices": len(vertices),
        "triangles": sum(len(triangles) for triangles in triangles_by_submesh),
        "submeshes": len(primitives),
        "bones": len(joint_ids),
        "skin_index": skin_index,
        "mesh_node": mesh_node_index,
    }


def build(args: argparse.Namespace) -> Path:
    index_path = args.index.resolve()
    game_dir = args.game_dir.resolve()
    pairs, selection = load_entries(index_path, game_dir, args.model)
    UnityPy.set_assetbundle_decrypt_key(bytes.fromhex(args.key))
    paths = list(dict.fromkeys(str(path) for _, path in pairs))
    print(f"Loading {len(paths)} role bundles for {args.model}", flush=True)
    env = UnityPy.Environment(*paths)
    prefab_path = next(path for entry, path in pairs if entry["logical_path"] == selection["prefab"]["logical_path"])
    prefab_file = next(iter(env.files[str(prefab_path)].files.values()))
    prefab_objects = [obj for obj in env.objects if obj.assets_file is prefab_file]
    transforms, _, component_to_transform, transform_names = transform_maps(prefab_objects)
    report_path = args.sample_dir.resolve() / "sample.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    builder = GltfBuilder(
        args.model,
        {
            "source_prefab": selection["prefab"]["logical_path"],
            "source_report": report_path.name if report_path.is_file() else None,
            "controller_prefix": args.controller_prefix,
            "animation_controllers": [
                {"name": item.get("name"), "clip_count": item.get("resolved_clip_count"), "layers": item.get("layer_count")}
                for item in report.get("animator_controllers", [])
            ],
            "notes": [
                "Skinned mesh, textures, Transform hierarchy, and Unity bind poses are embedded.",
                "Portable unlit base-color materials preserve alpha blending and culling; custom Unity shader lighting/effects are not reproduced.",
                "Selected Unity Transform animation curves are converted to standard glTF animation channels.",
            ],
        },
    )
    node_by_transform = add_transform_nodes(builder, transforms, transform_names)
    avatar = None
    for prefab_object in prefab_objects:
        if prefab_object.type.name == "Animator":
            avatar = resolve(getattr(prefab_object.read(), "m_Avatar", None))
            if avatar is not None:
                break
    path_nodes = animation_path_nodes(builder, transforms, transform_names, node_by_transform, avatar)
    renderer_stats: list[dict[str, Any]] = []
    used_meshes: set[tuple] = set()
    for obj in prefab_objects:
        if obj.type.name != "SkinnedMeshRenderer":
            continue
        renderer = obj.read()
        mesh = resolve(getattr(renderer, "m_Mesh", None))
        owner_transform_id = component_to_transform.get(obj.path_id)
        if not renderer.m_Enabled or not active_transform(owner_transform_id, transforms):
            continue
        if mesh is None:
            raise ValueError(f"Unresolved mesh for renderer {transform_names.get(owner_transform_id)}")
        renderer_stats.append(add_renderer(builder, renderer, mesh, owner_transform_id, node_by_transform, transform_names))
        used_meshes.add(asset_key(mesh))
    if not renderer_stats:
        raise ValueError("No active skinned renderers were exported")
    builder.gltf["extras"]["renderer_stats"] = renderer_stats
    builder.gltf["extras"]["transform_count"] = len(transforms)
    builder.gltf["extras"]["mesh_count"] = len(used_meshes)
    available_clips = matching_animation_clips(env, args.controller_prefix or args.model)
    selected_clips: list[Any] = []
    if args.all_clips:
        selected_clips = available_clips
    elif args.clip:
        for requested in args.clip:
            matches = [clip for clip in available_clips if object_name(clip).lower() == requested.lower()]
            if not matches:
                raise ValueError(f"AnimationClip not found: {requested!r}")
            selected_clips.extend(matches)
    else:
        preferred = next(
            (clip for clip in available_clips if object_name(clip).lower() == "stand1"),
            next((clip for clip in available_clips if object_name(clip).lower() == "run"), None),
        )
        if preferred is not None:
            selected_clips = [preferred]
    animation_stats: list[dict[str, Any]] = []
    name_counts: dict[str, int] = {}
    for clip in selected_clips:
        clip_name = object_name(clip) or "AnimationClip"
        name_counts[clip_name] = name_counts.get(clip_name, 0) + 1
        animation_name = clip_name if name_counts[clip_name] == 1 else f"{clip_name}_{name_counts[clip_name]}"
        try:
            animation_stats.append(add_animation(builder, clip, path_nodes, animation_name))
        except ValueError as exc:
            if args.all_clips:
                print(f"warning: skipped animation {clip_name}: {exc}", file=sys.stderr)
            else:
                raise
    builder.gltf["extras"]["available_animation_clip_count"] = len(available_clips)
    builder.gltf["extras"]["animation_stats"] = animation_stats
    output = args.output.resolve()
    builder.finish(output)
    print(json.dumps({
        "output": str(output),
        "bytes": output.stat().st_size,
        "nodes": len(builder.gltf["nodes"]),
        "skins": len(builder.gltf["skins"]),
        "meshes": len(builder.gltf["meshes"]),
        "materials": len(builder.gltf["materials"]),
        "textures": len(builder.gltf["textures"]),
        "renderers": len(renderer_stats),
        "animations": len(animation_stats),
        "animation_channels": sum(item["channels"] for item in animation_stats),
    }, ensure_ascii=False, indent=2), flush=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="r5kalienina")
    parser.add_argument(
        "--controller-prefix",
        help="controller family prefix; use the *Display family for main-screen idle/interaction clips",
    )
    parser.add_argument("--game-dir", type=Path, default=DEFAULT_GAME)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--sample-dir", type=Path, help="existing model_samples/<model> directory containing sample.json")
    parser.add_argument("--output", type=Path, help="output GLB path; defaults to sample-dir/<controller>_<clip>.glb")
    parser.add_argument("--clip", action="append", help="AnimationClip name to embed; may be repeated")
    parser.add_argument("--all-clips", action="store_true", help="embed every clip referenced by the selected controller family")
    parser.add_argument("--key", default=DEFAULT_KEY)
    args = parser.parse_args()
    if len(args.key) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", args.key):
        parser.error("--key must be an even-length hexadecimal string")
    if args.sample_dir is None:
        args.sample_dir = DEFAULT_INDEX.parent / "model_samples" / safe_name(args.model)
    if args.output is None:
        suffix = "all" if args.all_clips else safe_name(args.clip[0] if args.clip else "Stand1")
        name = f"{safe_name(args.controller_prefix or args.model)}_{suffix}"
        args.output = args.sample_dir / f"{name}.glb"
    build(args)


if __name__ == "__main__":
    main()
