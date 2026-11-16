from copy import deepcopy
import base64
import hashlib
import io
import json

import mongomock
from PIL import Image
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
import pytest

from app.services import editable_projects as service


def image_url():
    output = io.BytesIO()
    Image.new('RGB', (64, 48), 'green').save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()


def font_fixture():
    glyphs = {code: f'u{code:04X}' for code in range(32, 127)}
    names = ['.notdef', *glyphs.values()]
    font = FontBuilder(1000, isTTF=True)
    font.setupGlyphOrder(names)
    font.setupCharacterMap(glyphs)
    font.setupGlyf({name: TTGlyphPen(None).glyph() for name in names})
    font.setupHorizontalMetrics({name: (600, 0) for name in names})
    font.setupHorizontalHeader(ascent=800, descent=-200)
    font.setupNameTable({'familyName': 'Fixture', 'styleName': 'Regular', 'uniqueFontIdentifier': 'Fixture', 'fullName': 'Fixture', 'psName': 'Fixture'})
    font.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    font.setupPost()
    out = io.BytesIO(); font.save(out); raw = out.getvalue()
    return {'id': 'Sans', 'family': 'Fixture', 'source': 'data:font/ttf;base64,' + base64.b64encode(raw).decode(),
            'sha256': hashlib.sha256(raw).hexdigest(), 'glyphs': list(glyphs)}


@pytest.fixture
def fixture(settings, store):
    db = mongomock.MongoClient().db
    db.merchants.insert_one({'_id': 'm1', 'name': 'Imported Merchant'})
    db.whole_tasks.insert_one({'_id': 'task1', 'merchant_id': 'm1', 'input': {'reference_asset_ids': ['photo1']}})
    db.whole_versions.insert_one({'_id': 'v1', 'task_id': 'task1', 'merchant_id': 'm1', 'outputs': [{'id': 'o1', 'asset_id': 'poster1'}]})
    doc = service.create(db, task_id='task1', merchant_id='m1', source_version_id='v1', source_output_id='o1', request_id='request1')
    raw = base64.b64decode(image_url().split(',')[1])
    project = {'schema': 'image-editor-v2', 'width': 64, 'height': 48, 'title': 'Test',
               'assets': {'background': image_url()}, 'fonts': [font_fixture()],
               'provenance': {'background': {'role': 'background', 'source': 'local', 'sha256': hashlib.sha256(raw).hexdigest()}},
               'layers': [
                   {'id': 'bg', 'name': '底图', 'type': 'background', 'assetId': 'background', 'width': 64, 'height': 48, 'x': 32, 'y': 24, 'scale': 1, 'rotation': 0, 'opacity': 1, 'visible': True},
                   {'id': 'price', 'name': '价格', 'type': 'text', 'width': 40, 'height': 16, 'x': 32, 'y': 24, 'scale': 1, 'rotation': 0, 'opacity': 1, 'visible': True,
                    'text': {'value': '$59.99', 'font': 'Sans', 'fontSize': 12, 'width': 40, 'lineHeight': 1.2, 'color': '#ffffff', 'strokeColor': '#000000', 'strokeWidth': 0}}
               ]}
    return db, settings, store, doc, project


def test_creation_is_idempotent_and_bound_to_candidate(fixture):
    db, _, _, doc, _ = fixture
    again = service.create(db, task_id='task1', merchant_id='m1', source_version_id='v1', source_output_id='o1', request_id='request1')
    assert doc['_id'] == again['_id']
    assert db.whole_editable_projects.count_documents({}) == 1
    with pytest.raises(service.ProjectError):
        service.create(db, task_id='task1', merchant_id='m2', source_version_id='v1', source_output_id='o1', request_id='request2')
    with pytest.raises(service.ProjectError):
        service.create(db, task_id='task1', merchant_id='m1', source_version_id='v1', source_output_id='wrong', request_id='request2')


def test_save_restore_keeps_user_text_and_uses_small_db_metadata(fixture):
    db, settings, store, doc, project = fixture
    service.install(db, settings, store, doc, project, editor_html=b'<!doctype html><title>editor</title>', report={'status': 'PASS'})
    current = service.get(db, store, doc['_id'], 'm1')
    assert current['project']['layers'][1]['text']['value'] == '$59.99'
    changed = deepcopy(current['project'])
    changed['layers'][1]['text']['value'] = '$69.99'
    changed['layers'][1]['x'] = 36
    saved = service.save(db, settings, store, doc['_id'], 'm1', current['revision'], changed)
    restored = service.get(db, store, doc['_id'], 'm1')
    assert restored['project']['layers'][1]['text']['value'] == '$69.99'
    assert restored['project']['layers'][1]['x'] == 36
    assert saved['revision'] == current['revision'] + 1
    metadata = db.whole_editable_projects.find_one({'_id': doc['_id']})
    assert 'data:image/' not in json.dumps(metadata, default=str)
    assert metadata['source_version_id'] == 'v1'
    with pytest.raises(service.ProjectError) as error:
        service.save(db, settings, store, doc['_id'], 'm1', current['revision'], changed)
    assert error.value.status_code == 409
    with pytest.raises(service.ProjectError):
        service.get(db, store, doc['_id'], 'm2')


def test_bad_state_or_storage_failure_does_not_replace_last_good_project(fixture, monkeypatch):
    db, settings, store, doc, project = fixture
    service.install(db, settings, store, doc, project, editor_html=b'<html></html>', report={'status': 'PASS'})
    current = service.get(db, store, doc['_id'], 'm1')
    bad = deepcopy(project)
    bad['layers'][1]['x'] = float('nan')
    with pytest.raises(service.ProjectError):
        service.save(db, settings, store, doc['_id'], 'm1', current['revision'], bad)
    bad = deepcopy(project)
    bad['assets']['background'] = 'https://example.com/foreign.png'
    with pytest.raises(service.ProjectError):
        service.save(db, settings, store, doc['_id'], 'm1', current['revision'], bad)
    monkeypatch.setattr(store, 'put_bytes', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        service.save(db, settings, store, doc['_id'], 'm1', current['revision'], project)
    assert service.get(db, store, doc['_id'], 'm1')['revision'] == current['revision']


def test_nonpass_reconstruction_cannot_be_installed(fixture):
    db, settings, store, doc, project = fixture
    with pytest.raises(service.ProjectError):
        service.install(db, settings, store, doc, project, editor_html=b'<html></html>', report={'status': 'PENDING'})
    assert db.whole_editable_projects.find_one({'_id': doc['_id']})['status'] == 'queued'


def test_source_snapshot_includes_selected_edit_lineage(fixture):
    db, _, _, _, _ = fixture
    db.whole_versions.insert_one({'_id': 'v2', 'task_id': 'task1', 'merchant_id': 'm1', 'parent_version_id': 'v1',
        'operation': 'original_replace', 'instruction': '替换主菜', 'replacements': [{'asset_id': 'new-photo', 'target': '主菜'}],
        'model_input': {'images': [{'asset_id':'poster1', 'role':'替换底图'}, {'asset_id':'new-photo','role':'主菜'}]},
        'outputs':[{'id':'o2','asset_id':'poster2'}]})
    db.whole_versions.insert_one({'_id': 'v3', 'task_id': 'task1', 'merchant_id': 'm1', 'parent_version_id': 'v2',
        'operation': 'edit', 'instruction': '增加甜品', 'model_input': {'images': [{'asset_id':'dessert','role':'edit_reference'}]},
        'outputs':[{'id':'o3','asset_id':'poster3'}]})
    new = service.create(db, task_id='task1', merchant_id='m1', source_version_id='v3', source_output_id='o3', request_id='new')
    assert set(new['input_snapshot']['reference_asset_ids']) == {'photo1','new-photo','dessert'}
    assert [item['version_id'] for item in new['input_snapshot']['version_lineage']] == ['v1','v2','v3']
