"""Package reviewed native new-text sticker and final model image without recompositing."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil

from PIL import Image, ImageChops

from build_editor import image_info, region_mask, sha
from record_generation import validate_record


def package_text_edit(manifest, output_dir, base_dir=None):
    if isinstance(manifest, (str, Path)):
        path = Path(manifest).resolve()
        base_dir = path.parent
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
    m = deepcopy(manifest)
    base, out = Path(base_dir or '.').resolve(), Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('Use a new empty output directory')
    if m.get('mode') != 'text':
        raise ValueError('Explicit text mode is required')
    if not all(isinstance(m.get(k), str) and m[k].strip() for k in ('old_text', 'new_text')):
        raise ValueError('Record exact old_text and new_text')
    if m['old_text'] == m['new_text']:
        raise ValueError('new_text must differ from old_text')
    paths = {k: (base / m[k]).resolve() for k in ('poster', 'sticker', 'final')}
    poster, _ = image_info(paths['poster'])
    sticker, _ = image_info(paths['sticker'])
    final, _ = image_info(paths['final'])
    if final.size != poster.size:
        raise ValueError('Final image must keep the exact original poster canvas')
    for kind, path in (('sticker', paths['sticker']), ('final', paths['final'])):
        with Image.open(path) as raw:
            if raw.format != 'PNG':
                raise ValueError(kind + ' must be the native PNG output')
    if ('A' not in sticker.getbands() or sticker.getchannel('A').getextrema()[0] != 0
            or not sticker.getchannel('A').getbbox()):
        raise ValueError('New-text sticker needs visible content and native transparency')
    if final.convert('RGBA').getchannel('A').getextrema() != (255, 255):
        raise ValueError('Final poster must be fully opaque')
    edit = region_mask(m['edit_region'], poster.size, base, 'edit_region')
    records, reviews = {}, {}
    for kind, purpose, refs in [('sticker', 'text-sticker', [paths['poster']]),
                                ('final', 'text-final', [paths['poster'], paths['sticker']])]:
        record_path, review_path = base / m[kind + '_generation'], base / m[kind + '_review']
        record = json.loads(record_path.read_text(encoding='utf-8-sig'))
        validate_record(record, paths[kind], purpose, refs)
        review = json.loads(review_path.read_text(encoding='utf-8-sig'))
        if (review.get('status') != 'PASS' or review.get('image_sha256') != sha(paths[kind])
                or not isinstance(review.get('notes'), str) or not review['notes'].strip()):
            raise ValueError('Visual review is pending, failed or stale: ' + kind)
        records[kind], reviews[kind] = record_path, review_path
    generation = m.get('generation', {'new_calls': 0, 'upstream_calls': 2, 'reports': []})
    for key in ('new_calls', 'upstream_calls'):
        value = generation.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('Invalid generation call count')
    extra_reports = []
    for file in generation.get('reports', []):
        path = (base / file).resolve()
        json.loads(path.read_text(encoding='utf-8-sig'))
        extra_reports.append(path)
    # Report drift outside the authorized region; this is not a semantic visual check.
    diff = ImageChops.difference(poster.convert('RGBA'), final.convert('RGBA'))
    changed = Image.new('L', poster.size)
    for channel in diff.split():
        changed = ImageChops.lighter(changed, channel)
    outside = ImageChops.multiply(changed, ImageChops.invert(edit))
    changed_count = poster.width * poster.height - outside.histogram()[0]
    out.mkdir(parents=True, exist_ok=True)
    (out / 'inputs').mkdir()
    replay = deepcopy(m)
    for key, source in paths.items():
        target = out / ('inputs/poster' + source.suffix.lower() if key == 'poster' else key + '.png')
        shutil.copy2(source, target)
        replay[key] = target.relative_to(out).as_posix()
    for kind in ('sticker', 'final'):
        for suffix, source in (('_generation', records[kind]), ('_review', reviews[kind])):
            name = kind + suffix.replace('_', '-') + '.json'
            shutil.copy2(source, out / name)
            replay[kind + suffix] = name
    report_files = []
    for index, source in enumerate(extra_reports, 1):
        name = f'generation-{index}.json'
        shutil.copy2(source, out / name)
        report_files.append(name)
    replay['generation'] = {'new_calls': 0,
                            'upstream_calls': generation.get('new_calls', 0) + generation.get('upstream_calls', 0),
                            'reports': report_files}
    edit.save(out / 'edit-region.png')
    replay['edit_region'] = {'mask': 'edit-region.png'}
    (out / 'manifest.json').write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding='utf-8')
    report = {'mode': 'text', 'canvas_size': list(poster.size),
              'old_text': m['old_text'], 'new_text': m['new_text'],
              'outside_edit_changed_pixels': changed_count, 'original_pixels_locked': False,
              'final_sha256': sha(paths['final']), 'sticker_sha256': sha(paths['sticker']),
              'visual_review': 'provided_PASS_records_matched_hashes',
              'declared_generation': generation,
              'package_model_calls': 0, 'package_network_calls': 0,
              'limitations': ['Model-generated final; hash checks do not prove semantic quality or unchanged typography.']}
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package_text_edit(args.manifest, args.out), ensure_ascii=False, indent=2))
