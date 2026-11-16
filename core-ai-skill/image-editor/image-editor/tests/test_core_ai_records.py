from io import BytesIO
import json
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import record_core_ai_generation as core
from build_editor import build_editor, sha
from package_text_edit import package_text_edit
from test_builder import fixture
from test_extended_modes import background_manifest
from test_text_edit import text_case


RESULT = 'Image generated.\n![Generated Image](https://example.test/native.png)\nmedia_id: synthetic-only\n'


def test_core_record_preserves_native_reply_and_matches_background_builder(fixture):
    m, base = background_manifest(fixture)
    record = core.record_core_ai_generation(base / 'template.png', base / 'core.json',
        tool_result=RESULT, purpose='background-foreground', prompt='Remove the old scene.',
        model='gpt-image-2.5-sunburst', background='transparent', references=[base / 'poster.png'],
        review_out=base / 'core-review.json')
    assert record['tool'] == 'generate_image' and record['transport'] == 'core_ai_native_tool'
    assert record['sha256'] == sha(base / 'template.png')
    assert record['tool_result'] == RESULT and record['media_id'] == 'synthetic-only'
    assert record['references'][0]['sha256'] == sha(base / 'poster.png')
    assert json.loads((base / 'core-review.json').read_text())['status'] == 'PENDING'
    m['generation']['reports'] = ['core.json']
    build_editor(m, base / 'result', base)
    copied = json.loads((base / 'result/generation-1.json').read_text())
    assert copied['tool_result'] == RESULT
    build_editor(base / 'result/manifest.json', base / 'replay')


@pytest.mark.parametrize('result', ['Error: failed', 'Image generated.\nNo image link',
    RESULT + '![Another](https://example.test/second.png)\n',
    'Image generated.\n![Image](data:image/png;base64,fake)\n'])
def test_invalid_or_ambiguous_tool_reply_is_not_success(fixture, result):
    _, base = fixture
    with pytest.raises(ValueError):
        core.record_core_ai_generation(base / 'template.png', base / 'bad.json', tool_result=result,
            purpose='background-foreground', prompt='Remove scene.', model='test', background='transparent')
    assert not (base / 'bad.json').exists()


def test_native_png_and_transparency_failures_do_not_create_success_records(fixture):
    _, base = fixture
    for name, purpose, bg in [('red.jpg', 'text-final', 'opaque'),
                               ('blue.png', 'text-sticker', 'transparent'),
                               ('template.png', 'dish-background', 'opaque')]:
        with pytest.raises(ValueError):
            core.record_core_ai_generation(base / name, base / 'bad.json', tool_result=RESULT,
            purpose=purpose, prompt='Test output.', model='gpt-image-2.5-sunburst', background=bg)
        assert not (base / 'bad.json').exists()


def test_native_two_stage_receipts_work_with_text_packager(text_case):
    m, base = text_case
    for kind, bg, refs in [('sticker', 'transparent', ['poster']),
                            ('final', 'opaque', ['poster', 'sticker'])]:
        name = kind + '-core.json'
        core.record_core_ai_generation(base / (kind + '.png'), base / name,
            tool_result=RESULT, purpose='text-' + kind, prompt='Replace the target text.',
            model='gpt-image-2.5-sunburst', background=bg,
            references=[base / (r + '.png') for r in refs])
        m[kind + '_generation'] = name
    report = package_text_edit(m, base / 'result', base)
    assert report['outside_edit_changed_pixels'] == 0
    package_text_edit(base / 'result/manifest.json', base / 'replay')


def test_download_only_reads_returned_link_and_keeps_bytes(monkeypatch, tmp_path):
    buffer = BytesIO(); Image.new('RGB', (12, 10), 'red').save(buffer, format='PNG')
    data = buffer.getvalue(); calls = []
    def open_result(url, timeout):
        calls.append((url, timeout)); return BytesIO(data)
    monkeypatch.setattr(core, 'urlopen', open_result)
    core.download_native(RESULT, tmp_path / 'native.png')
    assert (tmp_path / 'native.png').read_bytes() == data
    assert calls == [('https://example.test/native.png', 60)]
    with pytest.raises(FileExistsError): core.download_native(RESULT, tmp_path / 'native.png')


def test_existing_records_are_never_overwritten(fixture):
    _, base = fixture
    (base / 'keep.json').write_text('keep')
    with pytest.raises(FileExistsError):
        core.record_core_ai_generation(base / 'template.png', base / 'keep.json', tool_result=RESULT,
            purpose='photo-template', prompt='Remove scene.', model='gpt-image-2.5-sunburst', background='transparent')
    assert (base / 'keep.json').read_text() == 'keep'


def test_lower_quality_cannot_be_recorded_as_a_successful_new_generation(fixture):
    _,base=fixture
    with pytest.raises(ValueError,match='quality.*xhigh'):
        core.record_core_ai_generation(base/'template.png',base/'bad.json',tool_result=RESULT,
            purpose='photo-template',prompt='Remove scene.',model='test',background='transparent',quality='high')
    assert not (base/'bad.json').exists()


def test_other_image_models_cannot_be_accepted_for_new_generation(fixture):
    _,base=fixture
    with pytest.raises(ValueError,match='model.*gpt-image-2.5-sunburst'):
        core.record_core_ai_generation(base/'template.png',base/'bad-model.json',tool_result=RESULT,
            purpose='photo-template',prompt='Remove scene.',model='gpt-image-2',background='transparent')
    assert not (base/'bad-model.json').exists()
