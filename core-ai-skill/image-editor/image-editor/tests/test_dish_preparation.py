import json
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import build_dish_editor as builder
from test_dishes import case


def test_prepare_layout_batches_inputs_before_background_or_cutouts_exist(case):
    manifest, base = case
    manifest.pop('background')
    manifest['dishes'] = [{'id': 'dish-one', 'original': 'source.png', 'box': [30, 30, 100, 100]}]
    original = (base / 'source.png').read_bytes()
    result = builder.prepare_layout(manifest, base / 'layout', base)
    assert result['status'] == 'PENDING_visual_review'
    assert result['source_images'][0]['size'] == [40, 40]
    assert result['source_images'][0]['sha256'] == builder.sha(base / 'source.png')
    assert result['scope_check'] == 'not_provided'
    assert all((base / 'layout' / p).is_file() for p in result['review_sheets'])
    assert (base / 'source.png').read_bytes() == original
    assert not (base / 'layout/editor.html').exists()


def test_independent_removal_scope_catches_incomplete_repair_and_wrong_protection(case):
    manifest, base = case
    manifest['removal_scope'] = {'polygon': [[45, 35], [115, 35], [115, 95], [45, 95]]}
    manifest['repair_region'] = {'polygon': [[50, 40], [110, 40], [110, 90], [50, 90]]}
    with pytest.raises(ValueError, match='removal_scope'):
        builder.prepare_layout(manifest, base / 'bad', base)
    with pytest.raises(ValueError, match='removal_scope'):
        builder.compose_background(manifest, base)
    manifest['repair_region'] = {'polygon': [[40, 30], [120, 30], [120, 100], [40, 100]]}
    manifest['protected_regions'] = [{'polygon': [[70, 50], [90, 50], [90, 70], [70, 70]]}]
    with pytest.raises(ValueError, match='removal_scope'):
        builder.prepare_layout(manifest, base / 'bad-protection', base)
    assert not (base / 'bad/input-report.json').exists()


def test_prepared_background_has_batched_boundary_sheet_and_scope_replays(case):
    manifest, base = case
    manifest['removal_scope'] = {'polygon': [[45, 35], [115, 35], [115, 95], [45, 95]]}
    prepared = builder.prepare_background(manifest, base / 'review', base)
    assert prepared['review_sheets']
    for name in prepared['review_sheets']:
        with Image.open(base / 'review' / name) as sheet:
            assert sheet.width <= 960 and sheet.height <= 900
    review_path = base / 'review/background-review.json'
    review = json.loads(review_path.read_text())
    assert review['status'] == 'PENDING'
    review.update(status='PASS', notes='Synthetic fixture with exact known boundary.')
    review_path.write_text(json.dumps(review))
    manifest['background_review'] = 'review/background-review.json'
    builder.build_dish_editor(manifest, base / 'editor', base)
    replay = json.loads((base / 'editor/manifest.json').read_text(encoding='utf-8'))
    assert (base / 'editor' / replay['removal_scope']['mask']).is_file()
    builder.build_dish_editor(base / 'editor/manifest.json', base / 'replay')
    assert Image.open(base / 'replay/clean-background.png').tobytes() == Image.open(base / 'editor/clean-background.png').tobytes()


def test_many_sources_paginate_review_instead_of_creating_a_huge_image(case):
    manifest, base = case
    manifest['dishes'] = [{'id': f'dish-{i}', 'original': 'source.png'} for i in range(13)]
    report = builder.prepare_layout(manifest, base / 'layout', base)
    assert len(report['review_sheets']) == 2
    assert len(report['source_images']) == 13
    for name in report['review_sheets']:
        with Image.open(base / 'layout' / name) as sheet:
            assert sheet.width <= 960 and sheet.height <= 900


def test_layout_rejects_mixed_modes_before_requesting_background(case):
    manifest, base = case
    manifest['slots'] = [{'id': 'old-photo-window'}]
    with pytest.raises(ValueError, match='mix'):
        builder.prepare_layout(manifest, base / 'mixed', base)
    assert not (base / 'mixed').exists()
