import importlib.util
import json
from pathlib import Path
import sys

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from build_editor import build_editor, sha
from test_builder import fixture, read_payload


def background_manifest(fixture):
    m, base = fixture
    m.update(mode='background', slots=[{
        'id': 'scene', 'source': 'red.jpg',
        'polygon': [[0, 0], [160, 0], [160, 120], [0, 120]],
        'photo_core': {'polygon': [[10, 40], [30, 40], [30, 70], [10, 70]]},
    }])
    receipt = {'tool': 'test native image tool', 'purpose': 'background-foreground',
               'status': 'success', 'prompt': 'Remove the photographic scene only.',
               'sha256': sha(base / 'template.png')}
    (base / 'generation.json').write_text(json.dumps(receipt), encoding='utf-8')
    m['generation'] = {'new_calls': 1, 'reports': ['generation.json']}
    return m, base


def test_background_uses_generated_foreground_and_full_original_photo(fixture):
    m, base = background_manifest(fixture)
    build_editor(m, base / 'result', base)
    fixed = Image.open(base / 'result/poster-template.png')
    assert fixed.getpixel((5, 5)) == (32, 55, 24, 255)
    assert fixed.getpixel((20, 50))[3] == 0
    data = read_payload(base / 'result')
    assert data['mode'] == 'background' and len(data['layers']) == 1
    assert (base / 'result/inputs/scene.jpg').read_bytes() == (base / 'red.jpg').read_bytes()
    build_editor(base / 'result/manifest.json', base / 'replay')
    assert read_payload(base / 'replay') == data


def test_background_rejects_local_windows_and_unbound_generation(fixture):
    m, base = background_manifest(fixture)
    m['slots'][0]['polygon'][0] = [15, 0]
    with pytest.raises(ValueError, match='full.canvas'):
        build_editor(m, base / 'bad', base)
    m, base = background_manifest(fixture)
    m['generation'] = {}
    with pytest.raises(ValueError, match='generation'):
        build_editor(m, base / 'bad2', base)
    m, base = background_manifest(fixture)
    receipt = json.loads((base / 'generation.json').read_text())
    receipt['sha256'] = 'wrong'
    (base / 'generation.json').write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match='generation'):
        build_editor(m, base / 'bad3', base)


def test_elements_can_split_every_design_without_any_photo_slots(fixture):
    m, base = fixture
    Image.new('RGBA', (160, 120)).save(base / 'empty.png')
    m.update(mode='elements', element_request='Split every design group, including the logo',
             slots=[], template='empty.png', background='background.png',
             elements=[{'id': 'logo', 'source': 'art.png', 'box': [0, 0, 60, 40]}])
    build_editor(m, base / 'result', base)
    data = read_payload(base / 'result')
    assert [l['kind'] for l in data['layers']] == ['art']
    assert data['background'] and Image.open(base / 'result/poster-template.png').getbbox() is None
    build_editor(base / 'result/manifest.json', base / 'replay')
    assert read_payload(base / 'replay') == data


def test_elements_fixed_layer_may_be_opaque_when_no_photo_slots(fixture):
    m, base = fixture
    m.update(mode='elements', element_request='Move the label', slots=[],
             background='background.png', elements=[{'id': 'label', 'source': 'art.png', 'box': [0, 0, 60, 40]}])
    Image.new('RGBA', (160, 120), '#223e22').save(base / 'fixed.png')
    m['template'] = 'fixed.png'
    build_editor(m, base / 'result', base)
    assert read_payload(base / 'result')['layers'][0]['id'] == 'label'


def test_elements_keep_original_canvas(fixture):
    m, base = fixture
    m.update(mode='elements', element_request='Move the label', canvas_size=[320, 240],
             background='background.png', elements=[{'id': 'label', 'source': 'art.png', 'box': [0, 0, 60, 40]}])
    with pytest.raises(ValueError, match='original poster canvas'):
        build_editor(m, base / 'bad', base)
