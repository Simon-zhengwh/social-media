import json
from pathlib import Path
import sys

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
from record_generation import record_generation
from package_text_edit import package_text_edit
from build_editor import sha


@pytest.fixture
def text_case(tmp_path):
    Image.new('RGB', (100, 80), 'green').save(tmp_path / 'poster.png')
    final = Image.new('RGB', (100, 80), 'green')
    final.paste('red', (10, 10, 30, 30))
    final.save(tmp_path / 'final.png')
    sticker = Image.new('RGBA', (40, 30))
    sticker.paste('yellow', (3, 3, 37, 27))
    sticker.save(tmp_path / 'sticker.png')
    for name, purpose, refs in [('sticker', 'text-sticker', ['poster']),
                                ('final', 'text-final', ['poster', 'sticker'])]:
        record_generation(tmp_path / (name + '.png'), tmp_path / (name + '-generation.json'),
                          tool='test image tool', purpose=purpose, prompt='Change old to new.',
                          references=[tmp_path / (r + '.png') for r in refs])
        review = {'status': 'PASS', 'image_sha256': sha(tmp_path / (name + '.png')),
                  'notes': 'Synthetic fixture only; verified geometry and unchanged outer pixels.'}
        (tmp_path / (name + '-review.json')).write_text(json.dumps(review))
    m = {'mode': 'text', 'poster': 'poster.png', 'old_text': '旧字', 'new_text': '新字',
         'edit_region': {'polygon': [[10, 10], [30, 10], [30, 30], [10, 30]]},
         'sticker': 'sticker.png', 'final': 'final.png',
         'sticker_generation': 'sticker-generation.json', 'final_generation': 'final-generation.json',
         'sticker_review': 'sticker-review.json', 'final_review': 'final-review.json'}
    return m, tmp_path


def test_package_preserves_native_outputs_and_replays(text_case):
    m, base = text_case
    report = package_text_edit(m, base / 'result', base)
    assert report['outside_edit_changed_pixels'] == 0
    assert report['visual_review'] == 'provided_PASS_records_matched_hashes'
    assert (base / 'result/final.png').read_bytes() == (base / 'final.png').read_bytes()
    assert (base / 'result/sticker.png').read_bytes() == (base / 'sticker.png').read_bytes()
    package_text_edit(base / 'result/manifest.json', base / 'replay')
    assert (base / 'replay/final.png').read_bytes() == (base / 'final.png').read_bytes()


def test_unreviewed_sticker_or_changed_reference_cannot_be_packaged(text_case):
    m, base = text_case
    review = base / m['sticker_review']
    data = json.loads(review.read_text()); data['status'] = 'PENDING'; review.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='review'):
        package_text_edit(m, base / 'bad', base)
    data['status'] = 'PASS'; review.write_text(json.dumps(data))
    record = base / m['final_generation']
    data = json.loads(record.read_text()); data['references'] = []; record.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='reference'):
        package_text_edit(m, base / 'bad2', base)
    assert not (base / 'bad/final.png').exists()


def test_text_final_must_keep_original_dimensions(text_case):
    m, base = text_case
    Image.new('RGB', (100, 90)).save(base / 'final.png')
    with pytest.raises(ValueError, match='canvas'):
        package_text_edit(m, base / 'bad', base)


def test_recording_creates_pending_review_and_never_overwrites(text_case):
    _, base = text_case
    review = base / 'pending.json'
    result = record_generation(base / 'sticker.png', base / 'extra.json', tool='native tool',
                               purpose='text-sticker', prompt='Edit target only.',
                               references=[base / 'poster.png'], review_out=review)
    assert result['sha256'] == sha(base / 'sticker.png')
    assert json.loads(review.read_text())['status'] == 'PENDING'
    with pytest.raises(FileExistsError):
        record_generation(base / 'sticker.png', base / 'extra.json', tool='native tool',
                          purpose='text-sticker', prompt='Edit target only.')


def test_extra_failed_attempts_and_call_counts_survive_portable_replay(text_case):
    m, base = text_case
    (base / 'failed.json').write_text(json.dumps({'status': 'failed', 'error': 'test attempt'}))
    m['generation'] = {'new_calls': 3, 'upstream_calls': 0,
                       'reports': ['sticker-generation.json', 'failed.json', 'final-generation.json']}
    package_text_edit(m, base / 'result', base)
    replay = json.loads((base / 'result/manifest.json').read_text(encoding='utf-8'))
    assert replay['generation']['new_calls'] == 0
    assert replay['generation']['upstream_calls'] == 3
    assert all((base / 'result' / p).exists() for p in replay['generation']['reports'])
    package_text_edit(base / 'result/manifest.json', base / 'replay')
    assert json.loads((base / 'replay/report.json').read_text())['declared_generation']['upstream_calls'] == 3
