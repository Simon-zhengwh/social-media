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


def test_logo_drag_starts_in_a_transparent_part_of_its_box(fixture):
    playwright_api = pytest.importorskip('playwright.sync_api')

    manifest, base = fixture
    manifest.update(
        mode='elements', element_request='Move the logo', slots=[],
        background='background.png',
        elements=[
            {'id': 'title', 'source': 'art.png', 'box': [80, 0, 140, 40]},
            {'id': 'mark', 'role': 'logo', 'source': 'art.png', 'box': [0, 0, 60, 40]},
        ],
    )
    output = base / 'logo-editor'
    build_editor(manifest, output, base)

    with playwright_api.sync_playwright() as playwright:
        chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        browser = playwright.chromium.launch(headless=True, executable_path=str(chrome) if chrome.exists() else None)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.goto((output / 'editor.html').as_uri() + '?selftest=1')
        page.wait_for_selector('#stage[aria-busy=false]')
        stage = page.locator('#stage').bounding_box()
        x = stage['x'] + 4 * stage['width'] / 160
        y = stage['y'] + 4 * stage['height'] / 120
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x + 20, y + 12)
        page.mouse.up()
        assert page.locator('#layer-select').input_value() == 'mark'
        assert page.evaluate('editorTest.layers[1].x') > 30
        assert page.evaluate('editorTest.layers[0].x') == 110
        assert page.evaluate('editorTest.hit(84, 4)') == -1
        browser.close()


@pytest.mark.parametrize('native_filter', [True, False])
def test_photo_tone_changes_only_selected_photo_and_exports(fixture, native_filter):
    playwright_api = pytest.importorskip('playwright.sync_api')

    manifest, base = fixture
    Image.new('RGB', (80, 160), (80, 110, 150)).save(base / 'red.jpg')
    manifest.update(mode='elements', element_request='Move the logo', background='background.png',
                    elements=[{'id': 'logo', 'role': 'logo', 'source': 'art.png', 'box': [0, 0, 60, 25]}])
    output = base / 'tone-editor'
    build_editor(manifest, output, base)
    with playwright_api.sync_playwright() as playwright:
        chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
        browser = playwright.chromium.launch(headless=True, executable_path=str(chrome) if chrome.exists() else None)
        page = browser.new_page(accept_downloads=True)
        if not native_filter:
            page.add_init_script("delete CanvasRenderingContext2D.prototype.filter")
        page.goto((output / 'editor.html').as_uri() + '?selftest=1')
        page.wait_for_selector('#stage[aria-busy=false]')
        assert not page.locator('#photo-tone').is_visible()
        page.select_option('#layer-select', 'left')
        assert page.locator('#photo-tone').is_visible()
        before = page.evaluate("editorTest.makeCanvas().toDataURL()")

        def pixels():
            return page.evaluate("""() => {
              const ctx = stage.getContext('2d');
              return [[25,65],[110,65],[25,10]].map(([x,y])=>Array.from(ctx.getImageData(x,y,1,1).data));
            }""")

        initial = pixels()
        if not native_filter:
            page.locator('#brightness').evaluate("""el => {
                el.value = 15;
                el.dispatchEvent(new Event('input', {bubbles: true}));
            }""")
            assert pixels() == initial
            assert page.locator('#brightness-value').inner_text() == '+15'
        page.locator('#brightness').fill('20')
        brighter = pixels()
        assert all(brighter[0][i] > initial[0][i] for i in range(3))
        assert brighter[1:] == initial[1:]
        page.get_by_role('button', name='恢复原色').click()
        page.locator('#contrast').fill('20')
        adjusted = pixels()
        assert adjusted[0][0] < initial[0][0] < initial[0][2] < adjusted[0][2]
        assert adjusted[1:] == initial[1:]
        with page.expect_download() as download:
            page.locator('#download').click()
        with Image.open(download.value.path()) as exported:
            assert list(exported.convert('RGBA').getpixel((25,65))) == adjusted[0]
        page.select_option('#layer-select', 'right')
        assert page.locator('#contrast').input_value() == '0'
        page.select_option('#layer-select', 'left')
        assert page.locator('#contrast').input_value() == '20'
        page.get_by_role('button', name='恢复原色').click()
        assert page.evaluate("editorTest.makeCanvas().toDataURL()") == before
        browser.close()
