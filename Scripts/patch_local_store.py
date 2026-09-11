"""Prepare/apply a reversible local-recharge callback patch to the installed client.

Requires UnityPy, pycryptodome and msgpack. Only XPayManager.lua changes;
normal payment behavior remains in place unless the local server returns
LocalCompleted=true. Preparation never modifies the installed game.
"""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import msgpack
import UnityPy

MARKER = '-- AscNet local recharge completion'
KEY = bytes.fromhex('587865636f6472506547616b61326536')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def text_assets(env):
    return {obj.path_id: (obj.read().m_Name, digest(obj.get_raw_data()))
            for obj in env.objects if obj.type.name == 'TextAsset'}


def prepare(game, output):
    UnityPy.set_assetbundle_decrypt_key(KEY)
    base = game / 'PGR_Data' / 'StreamingAssets'
    index = UnityPy.load(str(base / 'document/matrix/index'))
    index_asset = next(obj.read() for obj in index.objects if obj.type.name == 'TextAsset')
    catalog = msgpack.unpackb(index_asset.m_Script.encode('utf-8', 'surrogateescape'), strict_map_key=False)[0]
    filename = catalog['assets/temp/lua/matrix.ab'][0]
    source = next(path for folder in ('document/matrix', 'resource/matrix')
                  if (path := base / folder / filename).is_file())
    original = source.read_bytes()
    env = UnityPy.load(original)
    before = text_assets(env)
    targets = [obj for obj in env.objects if obj.type.name == 'TextAsset' and obj.read().m_Name == 'XPayManager.lua']
    if len(targets) != 1:
        raise RuntimeError('Expected exactly one XPayManager.lua')
    target = targets[0]
    data = target.read()
    if MARKER in data.m_Script:
        raise RuntimeError('Client already has the local store patch')
    anchor = '            DoPay(productKey, res.GameOrder, template.GoodsId)'
    if data.m_Script.count(anchor) != 1:
        raise RuntimeError('Client callback changed; refusing an ambiguous patch')
    insertion = '''            -- AscNet local recharge completion
            if res.LocalCompleted then
                XDataCenter.KickOutManager.Unlock(XEnumConst.KICK_OUT.LOCK.RECHARGE, true)
                XUiManager.OpenUiObtain(res.RewardList or {})
                XEventManager.DispatchEvent(XEventId.EVENT_SUCCESS_PAY)
                return
            end
'''
    if '\r\n' in data.m_Script:
        insertion = insertion.replace('\n', '\r\n')
    data.m_Script = data.m_Script.replace(anchor, insertion + anchor)
    data.save()
    patched = env.file.save(packer='lz4')
    verified = UnityPy.load(patched)
    after = text_assets(verified)
    assert before.keys() == after.keys()
    assert [key for key in before if before[key] != after[key]] == [target.path_id]
    assert MARKER in next(obj.read().m_Script for obj in verified.objects if obj.path_id == target.path_id)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'original.bundle').write_bytes(original)
    (output / 'patched.bundle').write_bytes(patched)
    (output / 'XPayManager.patched.lua').write_text(data.m_Script, encoding='utf-8')
    (output / 'manifest.json').write_text(json.dumps({
        'source': str(source.resolve()), 'original_sha256': digest(original),
        'patched_sha256': digest(patched), 'verified_text_assets': len(before),
    }, indent=2), encoding='utf-8')
    print(f'Prepared and verified one changed Lua script among {len(before)} TextAssets: {output}')


def apply(output, restore=False):
    if os.name == 'nt':
        processes = subprocess.check_output(['tasklist', '/FI', 'IMAGENAME eq PGR.exe', '/FO', 'CSV'], text=True)
        if 'PGR.exe' in processes:
            raise RuntimeError('Exit PGR before applying/restoring the client patch')
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    target = Path(manifest['source'])
    expected = manifest['patched_sha256' if restore else 'original_sha256']
    if digest(target.read_bytes()) != expected:
        raise RuntimeError('Installed bundle changed since preparation; refusing to overwrite')
    payload = (output / ('original.bundle' if restore else 'patched.bundle')).read_bytes()
    assert digest(payload) == manifest['original_sha256' if restore else 'patched_sha256']
    temporary = target.with_suffix(target.suffix + '.ascnet-store.tmp')
    temporary.write_bytes(payload)
    temporary.replace(target)
    assert digest(target.read_bytes()) == digest(payload)
    print('Restored original client bundle' if restore else 'Applied verified local store client patch')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--game-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--restore', action='store_true')
    args = parser.parse_args()
    if args.apply or args.restore:
        apply(args.output, restore=args.restore)
    else:
        if args.game_dir is None:
            parser.error('--game-dir is required for preparation')
        prepare(args.game_dir, args.output)
