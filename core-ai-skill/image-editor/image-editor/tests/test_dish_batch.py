import json
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import build_dish_editor as builder
from test_dishes import case


def new_dish(base, name='dish-two'):
    with Image.open(base / 'cutout.png') as image:
        image.getchannel('A').save(base / 'corrected-mask.png')
    return {'id': name, 'original': 'source.png', 'box': [40, 30, 110, 100],
            'segmentation': {'corrected_mask': 'corrected-mask.png'}}


def test_batch_prepares_multiple_dishes_without_approving_or_changing_inputs(case):
    manifest, base = case
    manifest['dishes'] = [new_dish(base), new_dish(base, 'dish-three')]
    source_before = (base / 'source.png').read_bytes()
    manifest_before = json.dumps(manifest, sort_keys=True)
    report = builder.prepare_assets(manifest, base / 'batch', base)
    assert report['status'] == 'PENDING_visual_review'
    assert report['image_generation_calls'] == 0
    assert len(report['dish_updates']) == 2
    for update in report['dish_updates']:
        with Image.open(update['cutout']) as cutout, Image.open(base / 'source.png') as source:
            assert cutout.size == source.size
            assert cutout.convert('RGB').tobytes() == source.convert('RGB').tobytes()
        review = json.loads(Path(update['cutout_review']).read_text())
        assert review['status'] == 'PENDING'
        assert review['cutout_sha256'] == builder.sha(Path(update['cutout']))
    assert (base / 'source.png').read_bytes() == source_before
    assert json.dumps(manifest, sort_keys=True) == manifest_before
    assert not (base / 'batch/editor.html').exists()
    for path in report['review_sheets']:
        with Image.open(path) as image:
            assert image.width <= 960 and image.height <= 900
    assert 'chromium' in report['runtime']


def test_batch_reuses_verified_cutout_and_builder_accepts_updates(case):
    manifest, base = case
    manifest['dishes'].append(new_dish(base))
    cutout_before = (base / 'cutout.png').read_bytes()
    report = builder.prepare_assets(manifest, base / 'batch', base)
    assert report['dish_updates'][0]['action'] == 'reused'
    assert report['dish_updates'][1]['action'] == 'segmented'
    assert (base / 'cutout.png').read_bytes() == cutout_before
    for dish, update in zip(manifest['dishes'], report['dish_updates']):
        dish.update({key: update[key] for key in ('cutout', 'segmentation_report', 'cutout_review')})
    with pytest.raises(ValueError, match='Visual review has not passed'):
        builder.build_dish_editor(manifest, base / 'unapproved', base)
    review_path = Path(manifest['dishes'][1]['cutout_review'])
    review = json.loads(review_path.read_text())
    review.update(status='PASS', notes='Synthetic fixture with exact known square alpha.')
    review_path.write_text(json.dumps(review))
    builder.build_dish_editor(manifest, base / 'editor', base)
    assert (base / 'editor/editor.html').is_file()


def test_batch_rejects_stale_reuse_instead_of_skipping_review(case):
    manifest, base = case
    Image.new('RGB', (40, 40), 'red').save(base / 'source.png')
    with pytest.raises(ValueError, match='RGB|original source'):
        builder.prepare_assets(manifest, base / 'stale', base)
    assert not (base / 'stale/preparation.json').exists()


def test_batch_checks_scope_before_segmentation(case):
    manifest, base = case
    manifest['dishes'] = [new_dish(base)]
    manifest['removal_scope'] = {'polygon': [[0, 0], [10, 0], [10, 10], [0, 10]]}
    with pytest.raises(ValueError, match='removal_scope'):
        builder.prepare_assets(manifest, base / 'invalid', base)
    assert not (base / 'invalid').exists()


def test_batch_uses_declared_original_and_rejects_overwrite(case):
    manifest, base = case
    manifest['dishes'] = [new_dish(base)]
    manifest['dishes'][0]['segmentation']['image'] = 'different.png'
    with pytest.raises(ValueError, match='original'):
        builder.prepare_assets(manifest, base / 'wrong-source', base)
    manifest['dishes'][0]['segmentation'].pop('image')
    out = base / 'occupied'
    out.mkdir()
    (out / 'keep.txt').write_text('keep')
    with pytest.raises(FileExistsError):
        builder.prepare_assets(manifest, out, base)
    assert (out / 'keep.txt').read_text() == 'keep'


def test_batch_paginates_many_cutouts(case):
    manifest, base = case
    manifest['dishes'] = [new_dish(base, f'dish-{i}') for i in range(5)]
    report = builder.prepare_assets(manifest, base / 'many', base)
    assert len(report['dish_updates']) == 5
    assert len(report['review_sheets']) >= 3
    for path in report['review_sheets']:
        with Image.open(path) as image:
            assert image.width <= 960 and image.height <= 900


def test_runtime_finds_installed_windows_browser_without_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.importlib.util, 'find_spec', lambda name: None)
    monkeypatch.setattr(builder.shutil, 'which', lambda name: None)
    browser = tmp_path / 'Google/Chrome/Application/chrome.exe'
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b'synthetic executable path; never launched')
    monkeypatch.setenv('PROGRAMFILES', str(tmp_path))
    monkeypatch.delenv('PROGRAMFILES(X86)', raising=False)
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    assert builder.runtime_status()['chromium'] == str(browser)
