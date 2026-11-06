"""Freeze the existing five-case input manifest into local runtime storage; no remote calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

from PIL import Image, ImageOps


def prepare(manifest_path: Path, output: Path):
    if output.exists():
        raise FileExistsError('Benchmark directory already exists; do not silently replace fixed inputs')
    source = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    cases = []
    for case in source['cases']:
        files = []
        for index, item in enumerate(case['attachments']):
            path = Path(item['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('Original benchmark image hash changed: '+path.name)
            files.append((path, item, f'{case["id"]}/{index+1}.png'))
        cases.append((case, files))
    output.mkdir(parents=True)
    catalog = {'version': 'image-editor-20261104-v1', 'cases': []}
    for case, files in cases:
        images = []
        for source_path, item, relative in files:
            target = output / relative
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source_path, target)
            with Image.open(target) as raw:
                preview = ImageOps.exif_transpose(raw).convert('RGB')
                size = list(preview.size)
                preview.thumbnail((600, 600))
                preview.save(target.with_suffix('.jpg'), quality=88)
            images.append({'file': relative, 'name': item['name'], 'sha256': item['sha256'], 'size': size})
        catalog['cases'].append({'id': case['id'], 'label': case['label'], 'prompt': case['prompt'],
                                  'prompt_source_sha256': case['prompt_source_sha256'], 'images': images})
    (output/'manifest.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'cases': len(catalog['cases']), 'images': sum(len(c['images']) for c in catalog['cases']), 'output': str(output)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.manifest, args.out), ensure_ascii=False))
