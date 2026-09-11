"""Regression coverage for Unity-to-glTF skin streams and coordinate conversion."""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_character_glb as glb


class CharacterGlbTests(unittest.TestCase):
    def test_one_two_and_four_influences_have_complete_vec4_storage(self):
        for indices, weights, expected in [
            ([(2,)], None, [(1., 0., 0., 0.)]),
            ([(0, 2)], [(0.25, 0.75)], [(0.25, 0.75, 0., 0.)]),
            ([(0, 1, 2, 3)], [(1., 2., 3., 4.)], [(0.1, 0.2, 0.3, 0.4)]),
        ]:
            with self.subTest(indices=indices):
                j, w = glb.skin_attributes(indices, weights, 1, 4)
                self.assertEqual(w, expected)
                self.assertEqual(len(j[0]), 4)
                self.assertEqual(len(glb.pack_uints(list(j[0]), 5123)), 8)
                self.assertEqual(len(glb.pack_floats(w)), 16)
                self.assertEqual(j[0][:len(indices[0])], indices[0])

    def test_bad_skin_streams_fail_instead_of_fabricating_bindings(self):
        for indices, weights, count, bones in [
            ([(0, 1)], None, 1, 2), ([(2,)], [(1.,)], 1, 2),
            ([(0,)], [(0.,)], 1, 1), ([(0,)], [(float('nan'),)], 1, 1),
            ([(0,)], [(1.,)], 2, 1), ([(0, 1)], [(1.,)], 1, 2),
        ]:
            with self.subTest(indices=indices, weights=weights):
                with self.assertRaises(ValueError):
                    glb.skin_attributes(indices, weights, count, bones)
        self.assertEqual(glb.skin_attributes([(0, 99)], [(1., 0.)], 1, 1)[0], [(0, 0, 0, 0)])

    def test_accessor_rejects_short_buffer(self):
        b = glb.GltfBuilder('fixture', {})
        with self.assertRaises(ValueError):
            b.accessor(struct.pack('<HH', 0, 1), 5123, 'VEC4', 1)
        self.assertEqual(b.gltf['accessors'], [])

    def test_skeleton_root_is_common_ancestor_not_first_joint(self):
        nodes = [{'children': [1, 2]}, {}, {'children': [3]}, {}]
        self.assertEqual(glb.common_ancestor(nodes, [1, 3]), 0)
        self.assertEqual(glb.common_ancestor(nodes, [3, 2]), 2)
        with self.assertRaises(ValueError):
            glb.common_ancestor([{}, {}], [0, 1])

    def test_coordinate_change_includes_inverse_bind_matrix(self):
        matrix = NS(**{f'e{r}{c}': (1 if r == c else 0) for r in range(4) for c in range(4)})
        matrix.e03, matrix.e13, matrix.e23 = 2, 3, 4
        self.assertEqual(glb.matrix_values(matrix)[12:16], [-2., 3., 4., 1.])

    def test_asset_identity_is_file_scoped_and_survives_reread(self):
        f1, f2 = object(), object()
        def asset(f):
            return NS(object_reader=NS(assets_file=f, path_id=12))
        self.assertEqual(glb.asset_key(asset(f1)), glb.asset_key(asset(f1)))
        self.assertNotEqual(glb.asset_key(asset(f1)), glb.asset_key(asset(f2)))

    def test_rigid_mesh_export_is_skinned_and_uvs_and_winding_are_converted(self):
        matrix = NS(**{f'e{r}{c}': (1 if r == c else 0) for r in range(4) for c in range(4)})
        renderer = NS(m_Bones=[NS(m_PathID=123)], m_Materials=[], m_Name='Renderer')
        mesh = NS(m_Name='Rigid', m_BindPose=[matrix], m_SubMeshes=[NS(baseVertex=0)])
        handler = NS(process=lambda: None, m_Vertices=[(1, 0, 0), (0, 1, 0), (0, 0, 1)],
                     m_IndexBuffer=[0, 1, 2], m_Normals=[(0, 0, 2)]*3,
                     m_UV0=[(0, 0), (1, .25), (.5, 1)], m_BoneIndices=[(0,)]*3,
                     m_BoneWeights=None, get_triangles=lambda: [[(0, 1, 2)]])
        b = glb.GltfBuilder('fixture', {})
        b.gltf['nodes'] = [{'name': 'Bone'}]
        b.gltf['scenes'][0]['nodes'] = [0]
        with patch.object(glb, 'MeshHandler', return_value=handler):
            glb.add_renderer(b, renderer, mesh, 123, {123: 0}, {123: 'Bone'})
        prim = b.gltf['meshes'][0]['primitives'][0]
        def values(accessor, fmt):
            a = b.gltf['accessors'][accessor];v = b.gltf['bufferViews'][a['bufferView']]
            return struct.unpack(fmt, b.bin.data[v['byteOffset']:v['byteOffset']+v['byteLength']])
        self.assertEqual(values(prim['attributes']['TEXCOORD_0'], '<6f'), (0, 1, 1, .75, .5, 0))
        self.assertEqual(values(prim['indices'], '<3H'), (0, 2, 1))
        self.assertEqual(values(prim['attributes']['POSITION'], '<9f')[0], -1)
        self.assertEqual(values(prim['attributes']['WEIGHTS_0'], '<12f'), (1, 0, 0, 0)*3)
        self.assertNotIn('children', b.gltf['nodes'][0])
        self.assertEqual(b.gltf['scenes'][0]['nodes'], [0, 1])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'fixture.glb';b.finish(out);data = out.read_bytes()
            magic, version, length = struct.unpack_from('<4sII', data)
            self.assertEqual((magic, version, length), (b'glTF', 2, len(data)))
            size = struct.unpack_from('<I', data, 12)[0]
            parsed = json.loads(data[20:20+size])
            self.assertEqual(parsed['skins'][0]['skeleton'], 0)


if __name__ == '__main__':
    unittest.main()
