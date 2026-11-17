from io import BytesIO
from pathlib import Path

from PIL import Image
import pytest

from test_builder import builder, fixture, read_payload


def logo_manifest(fixture):
    m, base = fixture
    m.update(mode='elements', element_request='Keep the supplied official logo independently replaceable',
             background='background.png', elements=[{
                 'id': 'logo', 'role': 'logo', 'label': 'Logo', 'source': 'art.png', 'box': [0, 0, 60, 24],
             }])
    return m, base


def test_official_opaque_logo_keeps_original_bytes_and_contain_fit(fixture):
    m, base = logo_manifest(fixture)
    Image.new('RGB', (80, 40), '#123456').save(base / 'logo.jpg')
    m['elements'][0]['source'] = 'logo.jpg'
    builder.build_editor(m, base / 'result', base)
    logo = read_payload(base / 'result')['layers'][-1]
    assert logo['role'] == 'logo'
    assert logo['initial']['scale'] == 0.6
    assert (base / 'result/inputs/logo.jpg').read_bytes() == (base / 'logo.jpg').read_bytes()


def test_logo_can_be_replaced_without_changing_other_layers(fixture):
    playwright = pytest.importorskip('playwright.sync_api')
    m, base = logo_manifest(fixture)
    builder.build_editor(m, base / 'result', base)
    replacement = Image.new('RGBA', (80, 40), '#2040ff')
    replacement.putpixel((0, 0), (0, 0, 0, 0))
    replacement.save(base / 'new-logo.png')
    chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=str(chrome) if chrome.is_file() else None)
        try:
            page = browser.new_page()
            page.goto((base / 'result/editor.html').as_uri() + '?selftest=1')
            page.wait_for_function('()=>window.editorTest && editorTest.state.ready')
            page.select_option('#layer-select', 'logo')
            playwright.expect(page.locator('#upload')).to_be_enabled()
            playwright.expect(page.locator('#photo-upload')).to_be_visible()
            page.evaluate('editorTest.move(7, 5); editorTest.scaleAt(editorTest.layers[editorTest.state.selected].baseScale * 1.3)')
            page.locator('#rotation').evaluate("el=>{el.value=15;el.dispatchEvent(new Event('input',{bubbles:true}));}")
            before = page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.rotation,l.visible])')
            page.locator('#upload').set_input_files(base / 'new-logo.png')
            page.wait_for_function("()=>editorTest.layers[editorTest.state.selected].fileName === 'new-logo.png'")
            assert page.evaluate('editorTest.layers.map(l=>[l.x,l.y,l.rotation,l.visible])') == before
            assert page.evaluate('editorTest.layers[editorTest.state.selected].alpha.length') == 80 * 40 * 4
            assert page.evaluate('editorTest.layers[editorTest.state.selected].alpha[3]') == 0
            assert page.locator('[data-layer="logo"] img').get_attribute('src').startswith('blob:')
            with page.expect_download() as download:
                page.click('#download-element')
            with Image.open(download.value.path()) as exported:
                assert exported.size == replacement.size
                assert exported.convert('RGBA').tobytes() == replacement.tobytes()
            page.locator('#upload').set_input_files({'name': 'broken.png', 'mimeType': 'image/png', 'buffer': b'bad image'})
            page.wait_for_function("()=>document.getElementById('status').classList.contains('error')")
            assert page.evaluate('editorTest.layers[editorTest.state.selected].fileName') == 'new-logo.png'
            page.select_option('#layer-select', 'left')
            playwright.expect(page.locator('#upload')).to_be_enabled()
            page.locator('#upload').set_input_files(base / 'blue.png')
            page.wait_for_function("()=>editorTest.layers[editorTest.state.selected].fileName === 'blue.png'")
            assert page.evaluate("editorTest.layers.find(l=>l.id==='logo').fileName") == 'new-logo.png'
        finally:
            browser.close()


def test_empty_logo_is_rejected(fixture):
    m, base = logo_manifest(fixture)
    Image.new('RGBA', (80, 40)).save(base / 'empty-logo.png')
    m['elements'][0]['source'] = 'empty-logo.png'
    with pytest.raises(ValueError, match='visible'):
        builder.build_editor(m, base / 'result', base)
