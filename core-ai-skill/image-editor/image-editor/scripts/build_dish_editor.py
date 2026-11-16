"""Build 1..N free dish layers over an approved, fully restored opaque scene."""
from copy import deepcopy
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil

from PIL import Image, ImageChops

from build_editor import box, data_url, image_info, region_mask, sha
from dish_review import background_panels, layout_overlay, save_review_sheets


def load_manifest(manifest, base_dir=None):
    if isinstance(manifest, (str, Path)):
        path = Path(manifest).resolve()
        base_dir = path.parent
        manifest = json.loads(path.read_text(encoding='utf-8-sig'))
    return deepcopy(manifest), Path(base_dir or '.').resolve()


def scene_digest(image):
    image = image.convert('RGBA')
    header = f'{image.width}x{image.height}:RGBA\n'.encode('ascii')
    return hashlib.sha256(header + image.tobytes()).hexdigest()


def check_review(path, field, image_path):
    review = json.loads(path.read_text(encoding='utf-8-sig'))
    if review.get('status') != 'PASS':
        raise ValueError('Visual review has not passed: ' + str(path))
    if review.get(field) != sha(image_path):
        raise ValueError('Visual review hash does not match image: ' + str(path))
    return review


def layout_masks(manifest, poster, base):
    repair = region_mask(manifest['repair_region'], poster.size, base, 'repair_region')
    protection = Image.new('L', poster.size)
    protection_masks = []
    for spec in manifest.get('protected_regions', []):
        mask = region_mask(spec, poster.size, base, 'protected_regions')
        protection_masks.append(mask)
        protection = ImageChops.lighter(protection, mask)
    repair = ImageChops.multiply(repair, ImageChops.invert(protection))
    if not repair.getbbox():
        raise ValueError('Repair region is empty after protection')
    removal = None
    if manifest.get('removal_scope'):
        removal = region_mask(manifest['removal_scope'], poster.size, base, 'removal_scope')
        uncovered = ImageChops.multiply(removal, ImageChops.invert(repair))
        if uncovered.getbbox():
            raise ValueError('removal_scope is not fully covered by repair_region after protection; correct the layout before generation')
    return repair, protection, protection_masks, removal


def prepare_layout(manifest, output_dir, base_dir=None):
    manifest, base = load_manifest(manifest, base_dir)
    if manifest.get('mode') != 'dishes':
        raise ValueError('Explicit dishes mode is required')
    if manifest.get('slots') or manifest.get('elements'):
        raise ValueError('Do not mix old photo windows or art splitting into dishes mode')
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('Use a new empty output directory')
    poster_path = (base / manifest['poster']).resolve()
    poster, _ = image_info(poster_path)
    repair, protection, _, removal = layout_masks(manifest, poster, base)
    panels = [('Original poster', poster),
              ('Red=repair Cyan=protected', layout_overlay(poster, repair, protection))]
    sources = []
    items = manifest.get('dishes', [])
    ids = [item.get('id', '') for item in items]
    if not items or len(set(ids)) != len(ids) or any(not re.fullmatch('[a-z][a-z0-9-]*', value) for value in ids):
        raise ValueError('Need 1..N dishes with unique lowercase ASCII ids')
    for item in items:
        path = (base / item['original']).resolve()
        source, _ = image_info(path)
        if 'box' in item:
            box(item['box'], *poster.size, 'dish box')
        sources.append({'id': item['id'], 'file': item['original'], 'size': list(source.size),
                        'sha256': sha(path), 'mode': source.mode})
        panels.append(('Source ' + item['id'], source))
    out.mkdir(parents=True, exist_ok=True)
    repair.save(out / 'effective-repair-mask.png')
    protection.save(out / 'protection-mask.png')
    inpaint = Image.new('RGBA', poster.size, 'white')
    inpaint.putalpha(ImageChops.invert(repair))
    inpaint.save(out / 'inpaint-mask.png')
    report = {'status': 'PENDING_visual_review', 'poster_size': list(poster.size),
              'poster_sha256': sha(poster_path), 'source_images': sources,
              'repair_bbox': list(repair.getbbox()), 'repair_pixels': repair.histogram()[255],
              'scope_check': 'covered' if removal is not None else 'not_provided',
              'opencv_available': importlib.util.find_spec('cv2') is not None,
              'review_sheets': save_review_sheets(panels, out, 'layout-review'),
              'visual_review': 'not_performed', 'image_generation_calls': 0}
    (out / 'input-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def runtime_status():
    status = {name: importlib.util.find_spec(name) is not None
              for name in ('cv2', 'numpy', 'playwright')}
    status['chromium'] = None
    if status['playwright']:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as runtime:
                executable = Path(runtime.chromium.executable_path)
                if executable.is_file():
                    status['chromium'] = str(executable)
        except Exception as error:
            status['browser_probe_error'] = str(error)[:300]
    if not status['chromium']:
        status['chromium'] = next((path for name in ('chromium', 'chromium-browser', 'google-chrome', 'chrome', 'msedge')
                                   if (path := shutil.which(name))), None)
    if not status['chromium']:
        candidates = [Path(root) / suffix
                      for name in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')
                      if (root := os.environ.get(name))
                      for suffix in ('Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe')]
        status['chromium'] = next((str(path) for path in candidates if path.is_file()), None)
    return status


def checked_cutout(item, base):
    original_path = (base / item['original']).resolve()
    cutout_path = (base / item['cutout']).resolve()
    original, _ = image_info(original_path)
    cutout, _ = image_info(cutout_path)
    if original.size != cutout.size or cutout.convert('RGB').tobytes() != original.convert('RGB').tobytes():
        raise ValueError(item['id'] + ': cutout must preserve the full original RGB pixels; alpha-only segmentation')
    if 'A' not in cutout.getbands() or cutout.getchannel('A').getextrema()[0] != 0 or not cutout.getchannel('A').getbbox():
        raise ValueError(item['id'] + ': cutout needs visible content and true transparency')
    segmentation = json.loads((base / item['segmentation_report']).read_text(encoding='utf-8-sig'))
    if segmentation.get('method') not in ('grabcut_cpu', 'corrected_mask', 'existing_alpha'):
        raise ValueError(item['id'] + ': unsupported segmentation method')
    if segmentation.get('source_sha256') != sha(original_path) or segmentation.get('rgb_preserved') is not True:
        raise ValueError(item['id'] + ': segmentation report must match the original source')
    check_review((base / item['cutout_review']).resolve(), 'cutout_sha256', cutout_path)
    return original_path, cutout_path, cutout


def prepare_assets(manifest, output_dir, base_dir=None):
    """Batch layout, CPU cutouts and bounded review sheets; never approve or generate images."""
    from segment_photo import segment_photo

    manifest, base = load_manifest(manifest, base_dir)
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('Use a new empty output directory')
    layout = prepare_layout(manifest, out / 'layout', base)
    report = {'status': 'PENDING_visual_review', 'runtime': runtime_status(),
              'layout_report': str(out / 'layout/input-report.json'),
              'review_sheets': [str(out / 'layout' / name) for name in layout['review_sheets']],
              'dish_updates': [], 'image_generation_calls': 0}
    panels = []
    for item, source in zip(manifest['dishes'], layout['source_images']):
        original_path = (base / item['original']).resolve()
        if item.get('cutout'):
            _, cutout_path, cutout = checked_cutout(item, base)
            update = {key: str((base / item[key]).resolve())
                      for key in ('cutout', 'segmentation_report', 'cutout_review')}
            update['action'] = 'reused'
        else:
            spec = dict(item.get('segmentation', {}))
            if 'image' in spec:
                raise ValueError(item['id'] + ': segmentation uses the declared original; omit image')
            spec['image'] = str(original_path)
            spec.setdefault('source_sha256', source['sha256'])
            target = out / item['id']
            segmented = segment_photo(spec, target, base)
            cutout_path = target / 'cutout.png'
            cutout, _ = image_info(cutout_path)
            review_path = target / 'cutout-review.json'
            review_path.write_text(json.dumps({'status': 'PENDING', 'cutout_sha256': sha(cutout_path),
                                              'notes': ''}, indent=2), encoding='utf-8')
            update = {'cutout': str(cutout_path), 'segmentation_report': str(target / 'report.json'),
                      'cutout_review': str(review_path), 'action': 'segmented',
                      'boundary_status': segmented['status'], 'contacts_box_sides': segmented['contacts_box_sides']}
        update['id'] = item['id']
        report['dish_updates'].append(update)
        original, _ = image_info(original_path)
        original.thumbnail((308, 258), Image.Resampling.LANCZOS)
        panels.append(('Source ' + item['id'], original))
        thumb = cutout.copy()
        thumb.thumbnail((308, 258), Image.Resampling.LANCZOS)
        for color in ('black', 'white'):
            backing = Image.new('RGB', thumb.size, color)
            backing.paste(thumb, mask=thumb.getchannel('A'))
            panels.append((item['id'] + ' on ' + color, backing))
    report['review_sheets'] += [str(out / name) for name in save_review_sheets(panels, out, 'cutout-review')]
    (out / 'preparation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def compose_background(manifest, base):
    poster_path = (base / manifest['poster']).resolve()
    poster, _ = image_info(poster_path)
    poster = poster.convert('RGBA')
    background_path = (base / manifest['background']).resolve()
    background, _ = image_info(background_path)
    if background.size != poster.size:
        raise ValueError('Clean background must use the exact original canvas; register explicitly before building')
    if 'A' in background.getbands() and background.getchannel('A').getextrema() != (255, 255):
        raise ValueError('Clean background must be fully opaque')
    repair, protection, protection_masks, removal = layout_masks(manifest, poster, base)

    clean = Image.composite(background.convert('RGBA'), poster, repair)
    if clean.getchannel('A').getextrema() != (255, 255):
        raise ValueError('Final scene must be fully opaque')
    difference = ImageChops.difference(clean, poster)
    outside = ImageChops.invert(repair)
    if any(ImageChops.multiply(channel, outside).getbbox() for channel in difference.split()):
        raise ValueError('Original pixels changed outside repair')
    return {
        'poster_path': poster_path,
        'poster': poster,
        'background_path': background_path,
        'background': background,
        'clean': clean,
        'repair': repair,
        'protection': protection,
        'protection_masks': protection_masks,
        'removal_scope': removal,
    }


def prepare_background(manifest, output_dir, base_dir=None):
    manifest, base = load_manifest(manifest, base_dir)
    if manifest.get('mode') != 'dishes':
        raise ValueError('Explicit dishes mode is required')
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('Use a new empty output directory')
    scene = compose_background(manifest, base)
    review = {
        'status': 'PENDING',
        'background_sha256': sha(scene['background_path']),
        'scene_rgba_sha256': scene_digest(scene['clean']),
        'notes': '',
    }
    out.mkdir(parents=True, exist_ok=True)
    scene['clean'].convert('RGB').save(out / 'clean-background.png')
    sheets = save_review_sheets(background_panels(scene), out, 'background-review')
    (out / 'background-review.json').write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return {'status': 'PENDING_visual_review', 'preview': str(out / 'clean-background.png'), 'review_sheets': sheets}


def build_dish_editor(manifest, output_dir, base_dir=None):
    manifest, base = load_manifest(manifest, base_dir)
    out = Path(output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('Use a new empty output directory')
    if manifest.get('mode') != 'dishes':
        raise ValueError('Explicit dishes mode is required')
    if manifest.get('slots') or manifest.get('elements'):
        raise ValueError('Do not mix old photo windows or art splitting into dishes mode')
    items = manifest.get('dishes', [])
    ids = [item.get('id', '') for item in items]
    if not items or len(set(ids)) != len(ids) or any(not re.fullmatch('[a-z][a-z0-9-]*', value) for value in ids):
        raise ValueError('Need 1..N dishes with unique lowercase ASCII ids')

    scene = compose_background(manifest, base)
    poster = scene['poster']
    width, height = poster.size
    review = check_review(
        (base / manifest['background_review']).resolve(), 'background_sha256', scene['background_path']
    )
    if review.get('scene_rgba_sha256') != scene_digest(scene['clean']):
        raise ValueError('Final scene review hash does not match composed pixels; prepare and review the final scene again')

    prepared = []
    for item in items:
        original_path, cutout_path, cutout = checked_cutout(item, base)

        bounds = box(item['box'], width, height, 'dish box')
        left, top, right, bottom = cutout.getchannel('A').getbbox()
        scale = min((bounds[2] - bounds[0]) / (right - left), (bounds[3] - bounds[1]) / (bottom - top))
        initial = {
            'x': (bounds[0] + bounds[2]) / 2 + (cutout.width - left - right) * scale / 2,
            'y': (bounds[1] + bounds[3]) / 2 + (cutout.height - top - bottom) * scale / 2,
            'scale': scale,
            'baseScale': scale,
            'rotation': 0,
            'visible': True,
        }
        prepared.append((item, original_path, cutout_path, initial, bounds))

    generation = manifest.get('generation', {})
    for key in ('new_calls', 'upstream_calls'):
        value = generation.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError('Invalid generation call count')
    generation_paths = [(base / file).resolve() for file in generation.get('reports', [])]
    for path in generation_paths:
        json.loads(path.read_text(encoding='utf-8-sig'))

    # All input and review checks finish before creating a deliverable.
    out.mkdir(parents=True, exist_ok=True)
    (out / 'inputs').mkdir()

    def copy_input(path, name):
        target = out / 'inputs' / (name + path.suffix.lower())
        shutil.copy2(path, target)
        return target.relative_to(out).as_posix()

    replay = deepcopy(manifest)
    replay['poster'] = copy_input(scene['poster_path'], 'poster')
    replay['background'] = copy_input(scene['background_path'], 'background')
    replay['background_review'] = copy_input((base / manifest['background_review']).resolve(), 'background-review')
    scene['repair'].save(out / 'repair-mask.png')
    replay['repair_region'] = {'mask': 'repair-mask.png'}
    if scene['removal_scope'] is not None:
        scene['removal_scope'].save(out / 'removal-scope.png')
        replay['removal_scope'] = {'mask': 'removal-scope.png'}
    scene['clean'].convert('RGB').save(out / 'clean-background.png')
    front = poster.copy()
    front.putalpha(scene['protection'])
    front.save(out / 'fixed-foreground.png')
    for index, mask in enumerate(scene['protection_masks']):
        name = f'protected-{index}.png'
        mask.save(out / name)
        replay['protected_regions'][index] = {'mask': name}

    layers = []
    for (item, original_path, cutout_path, initial, bounds), saved in zip(prepared, replay['dishes']):
        for key, prefix in [('original', 'original'), ('cutout', 'cutout'), ('segmentation_report', 'segmentation'), ('cutout_review', 'review')]:
            saved[key] = copy_input((base / item[key]).resolve(), prefix + '-' + item['id'])
        saved['original_name'] = item.get('original_name', original_path.name)
        layers.append({
            'id': item['id'],
            'label': str(item.get('label', item['id'])),
            'kind': 'dish',
            'source': data_url(out / saved['cutout']),
            'originalSource': data_url(out / saved['original']),
            'backgroundMode': 'cutout',
            'bounds': bounds,
            'fileName': saved['original_name'],
            'text': '默认去背景，可切换保留原照背景；菜品RGB保留。',
            'initial': initial,
        })
    reports = [copy_input(path, f'generation-{index}') for index, path in enumerate(generation_paths)]
    replay['generation'] = {
        'new_calls': 0,
        'upstream_calls': generation.get('new_calls', 0) + generation.get('upstream_calls', 0),
        'reports': reports,
    }
    config = {
        'width': width,
        'height': height,
        'title': str(manifest.get('title', '菜品自由调整')),
        'mode': 'dishes',
        'background': data_url(out / 'clean-background.png'),
        'fixed': data_url(out / 'fixed-foreground.png'),
        'layers': layers,
    }
    payload = json.dumps(config, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e')
    shell = (Path(__file__).parents[1] / 'assets/dish-editor.html').read_text(encoding='utf-8')
    html = shell.replace('__EDITOR_DATA__', payload)
    report = {
        'mode': 'dishes',
        'dish_count': len(items),
        'canvas_size': list(poster.size),
        'complete_background': True,
        'scene_rgba_sha256': scene_digest(scene['clean']),
        'outside_repair_changed_pixels': 0,
        'source_rgb_preserved': True,
        'dish_background_variants': ['cutout', 'original'],
        'default_dish_background_mode': 'cutout',
        'old_shape_clips_subject': False,
        'visual_reviews': 'provided PASS records matched final scene and cutout hashes; semantic review performed separately',
        'html_build_model_calls': 0,
        'declared_generation': generation,
        'html_sha256': hashlib.sha256(html.encode('utf-8')).hexdigest(),
    }
    (out / 'manifest.json').write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'editor.html').write_bytes(html.encode('utf-8'))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument('--prepare-background', action='store_true', help='Write the final empty scene, boundary sheets and a PENDING review')
    phases.add_argument('--prepare-layout', action='store_true', help='Inspect all sources and repair/protection geometry before generation')
    phases.add_argument('--prepare-assets', action='store_true', help='Batch layout, all CPU cutouts, runtime status and PENDING review sheets')
    args = parser.parse_args()
    operation = build_dish_editor
    if args.prepare_background:
        operation = prepare_background
    elif args.prepare_layout:
        operation = prepare_layout
    elif args.prepare_assets:
        operation = prepare_assets
    print(json.dumps(operation(args.manifest, args.out), ensure_ascii=False, indent=2))
