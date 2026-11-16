import importlib.util
from io import BytesIO
import base64
import json
from pathlib import Path
import re

from PIL import Image
import pytest

SPEC=importlib.util.spec_from_file_location('editor_builder',Path(__file__).parents[1]/'scripts/build_editor.py')
builder=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.fixture
def fixture(tmp_path):
    Image.new('RGB',(160,120),'#223e22').save(tmp_path/'poster.png')
    template=Image.new('RGBA',(160,120))
    for x in range(160):
        for y in range(25):template.putpixel((x,y),(32,55,24,255))
    template.save(tmp_path/'template.png')
    Image.new('RGB',(80,160),'red').save(tmp_path/'red.jpg')
    Image.new('RGB',(130,100),'blue').save(tmp_path/'blue.png')
    Image.new('RGB',(100,100),'#224520').save(tmp_path/'background.png')
    art=Image.new('RGBA',(30,20));art.paste((240,180,30,255),(3,3,27,17));art.save(tmp_path/'art.png')
    return {'title':'Test poster','poster':'poster.png','template':'template.png','slots':[
        {'id':'left','source':'red.jpg','polygon':[[0,25],[79,25],[79,119],[0,119]],'source_crop':[0,20,80,140], 'photo_core':{'polygon':[[10,40],[30,40],[30,70],[10,70]]}},
        {'id':'right','source':'blue.png','polygon':[[80,25],[159,25],[159,119],[80,119]], 'photo_core':{'polygon':[[100,40],[130,40],[130,70],[100,70]]}}
    ]},tmp_path


def read_payload(out):
    return json.loads(re.search(r'<script id="editor-data" type="application/json">(.*?)</script>',(out/'editor.html').read_text(encoding='utf-8'),re.S).group(1))


def test_photo_mode_is_default_preserves_complete_original_and_replays(fixture):
    manifest,base=fixture
    out=base/'result'
    builder.build_editor(manifest,out,base)
    data=read_payload(out)
    assert data['mode']=='photos' and len(data['layers'])==2
    assert all(l['kind']=='photo' for l in data['layers'])
    assert base64.b64decode(data['layers'][0]['source'].split(',',1)[1])==(base/'red.jpg').read_bytes()
    assert data['layers'][0]['source_size']==[80,160]
    assert data['layers'][0]['initial']['scale']==data['layers'][0]['initial']['baseScale']
    replay=out/'manifest.json'
    builder.build_editor(replay,base/'replay')
    assert read_payload(base/'replay')==data


def test_elements_need_explicit_mode_and_user_request(fixture):
    m,base=fixture
    m['elements']=[{'id':'slogan','source':'art.png','box':[0,0,60,40]}]
    with pytest.raises(ValueError,match='explicit'):builder.build_editor(m,base/'bad',base)
    m['mode']='elements'
    with pytest.raises(ValueError,match='element_request'):builder.build_editor(m,base/'bad2',base)


def test_elements_require_a_fully_opaque_background(fixture):
    m,base=fixture
    m.update(mode='elements',element_request='Move the slogan',elements=[{'id':'slogan','source':'art.png','box':[0,0,60,40]}])
    with pytest.raises(ValueError,match='background'):builder.build_editor(m,base/'bad',base)
    m['background']='template.png'
    with pytest.raises(ValueError,match='opaque'):builder.build_editor(m,base/'bad2',base)
    m['background']='background.png'
    builder.build_editor(m,base/'ok',base)
    assert len(read_payload(base/'ok')['layers'])==3


def test_invalid_windows_and_overlap_are_rejected(fixture):
    m,base=fixture
    m['slots'][1]['polygon']=[[40,30],[140,30],[140,100],[40,100]]
    with pytest.raises(ValueError,match='overlap'):builder.build_editor(m,base/'bad',base)
    m['slots'][1]['polygon']=[[80,-20],[159,25],[159,119],[80,119]]
    with pytest.raises(ValueError,match='polygon'):builder.build_editor(m,base/'bad2',base)


def test_gates_are_disjoint_and_sources_do_not_crop(fixture):
    m,base=fixture
    builder.build_editor(m,base/'ok',base)
    payload=read_payload(base/'ok')
    masks=[Image.open(BytesIO(base64.b64decode(l['window'].split(',',1)[1]))).getchannel('A') for l in payload['layers']]
    assert all(mask.getbbox() for mask in masks)
    assert all(not(a and b) for a,b in zip(masks[0].tobytes(),masks[1].tobytes()))
    assert Image.open(base/'ok/inputs/left.jpg').size==(80,160)


def test_model_alpha_and_title_are_validated(fixture):
    m,base=fixture
    m['template']='blue.png'
    with pytest.raises(ValueError,match='alpha'):builder.build_editor(m,base/'bad',base)
    m['template']='template.png'
    m['title']='</script><script>alert(1)</script>'
    builder.build_editor(m,base/'ok',base)
    html=(base/'ok/editor.html').read_text(encoding='utf-8')
    assert m['title'] not in html and read_payload(base/'ok')['title']==m['title']


def test_nonempty_output_and_duplicate_ids_are_rejected(fixture):
    m,base=fixture
    out=base/'keep';out.mkdir();(out/'user-file.txt').write_text('keep')
    with pytest.raises(FileExistsError):builder.build_editor(m,out,base)
    m['slots'][1]['id']='left'
    with pytest.raises(ValueError,match='unique'):builder.build_editor(m,base/'bad',base)
    assert (out/'user-file.txt').read_text()=='keep'


def test_wrong_template_aspect_is_rejected_without_creating_html(fixture):
    m,base=fixture
    Image.open(base/'template.png').resize((100,200)).save(base/'wrong-ratio.png')
    m['template']='wrong-ratio.png'
    with pytest.raises(ValueError,match='aspect ratio'):builder.build_editor(m,base/'bad',base)
    assert not (base/'bad/editor.html').exists()


def test_fixed_layer_uses_the_entire_model_rgba_without_restoring_original(fixture):
    m,base=fixture
    im=Image.open(base/'template.png')
    # Both model RGB and alpha must survive outside the source-photo windows.
    im.paste((200,30,40,252),(0,0,160,25));im.save(base/'template.png')
    builder.build_editor(m,base/'ok',base)
    fixed=Image.open(base/'ok/poster-template.png')
    assert fixed.tobytes()==im.tobytes()
    assert fixed.getpixel((20,50))[3]==0


def test_missing_independent_photo_core_is_rejected(fixture):
    m,base=fixture
    del m['slots'][0]['photo_core']
    with pytest.raises(ValueError,match='photo_core'):builder.build_editor(m,base/'bad',base)


def test_photo_core_requires_exact_zero_alpha(fixture):
    m,base=fixture
    im=Image.open(base/'template.png');im.putpixel((20,50),(255,255,255,1));im.save(base/'template.png')
    with pytest.raises(ValueError,match='photo_core.*alpha'):builder.build_editor(m,base/'bad',base)
    assert not (base/'bad/editor.html').exists()


def test_photo_core_must_be_inside_window(fixture):
    m,base=fixture
    m['slots'][0]['photo_core']={'polygon':[[10,10],[20,10],[20,20],[10,20]]}
    with pytest.raises(ValueError,match='photo_core.*inside'):builder.build_editor(m,base/'bad',base)


def test_protected_graphics_are_kept_from_model_not_pasted_back_from_original(fixture):
    m,base=fixture
    original=Image.open(base/'poster.png');original.paste((245,230,200),(45,80,71,106));original.save(base/'poster.png')
    m['protected_regions']=[{'id':'price','polygon':[[45,80],[70,80],[70,105],[45,105]]}]
    native=Image.open(base/'template.png');native.paste((240,225,195,255),(45,80,71,106));native.save(base/'template.png')
    builder.build_editor(m,base/'ok',base)
    fixed=Image.open(base/'ok/poster-template.png')
    assert fixed.getpixel((55,90))==(240,225,195,255)
    assert fixed.getpixel((20,50))[3]==0
    assert json.loads((base/'ok/report.json').read_text())['protection']['original_pixels_locked'] is False
    builder.build_editor(base/'ok/manifest.json',base/'replay')
    assert Image.open(base/'replay/poster-template.png').tobytes()==fixed.tobytes()


def test_missing_protected_graphic_is_rejected_instead_of_restored(fixture):
    m,base=fixture
    m['protected_regions']=[{'id':'price','polygon':[[45,80],[70,80],[70,105],[45,105]]}]
    with pytest.raises(ValueError,match='protected.*template'):
        builder.build_editor(m,base/'bad',base)
    assert not (base/'bad/editor.html').exists()


def test_protected_graphic_may_keep_its_real_translucent_edge(fixture):
    m,base=fixture
    m['protected_regions']=[{'id':'brush','polygon':[[45,80],[70,80],[70,105],[45,105]]}]
    native=Image.open(base/'template.png')
    native.paste((240,225,195,128),(45,80,71,106));native.save(base/'template.png')
    builder.build_editor(m,base/'out',base)
    assert Image.open(base/'out/poster-template.png').getpixel((55,90))==(240,225,195,128)


def test_incomplete_window_cannot_revive_old_photo_or_silently_leave_a_slit(fixture):
    m,base=fixture
    # The native template erased the complete panel, but the hand-drawn gate is narrower.
    m['slots'][1]['polygon']=[[80,25],[150,25],[150,119],[80,119]]
    with pytest.raises(ValueError,match='windows.*cleared'):
        builder.build_editor(m,base/'bad',base)
    assert not (base/'bad/editor.html').exists()


def test_single_photo_can_use_model_transparency_without_a_hand_drawn_gate(fixture):
    m,base=fixture
    m['slots']=m['slots'][:1]
    del m['slots'][0]['polygon']
    builder.build_editor(m,base/'out',base)
    data=read_payload(base/'out')
    gate=Image.open(BytesIO(base64.b64decode(data['layers'][0]['window'].split(',',1)[1])))
    assert gate.getchannel('A').getextrema()==(255,255)
    assert data['layers'][0]['bounds']==[0,25,160,120]
    assert Image.open(base/'out/poster-template.png').tobytes()==Image.open(base/'template.png').tobytes()
    builder.build_editor(base/'out/manifest.json',base/'replay')
    assert read_payload(base/'replay')==data


def test_window_coverage_includes_translucent_photo_edges(fixture):
    m,base=fixture
    native=Image.open(base/'template.png')
    native.paste((20,30,40,128),(0,25,1,120));native.save(base/'template.png')
    m['slots'][0]['polygon']=[[1,25],[79,25],[79,119],[1,119]]
    with pytest.raises(ValueError,match='windows.*cleared'):
        builder.build_editor(m,base/'bad',base)


def test_default_photo_fit_covers_translucent_edges_without_using_nearly_opaque_header(fixture):
    m,base=fixture
    native=Image.open(base/'template.png')
    native.paste((20,30,40,252),(0,0,160,24))
    native.paste((20,30,40,128),(0,24,160,25));native.save(base/'template.png')
    m['slots']=m['slots'][:1];del m['slots'][0]['polygon']
    builder.build_editor(m,base/'out',base)
    assert read_payload(base/'out')['layers'][0]['bounds']==[0,24,160,120]


def test_protected_graphic_cannot_hide_bad_photo_core(fixture):
    m,base=fixture
    m['protected_regions']=[{'id':'wrong','polygon':[[10,40],[30,40],[30,70],[10,70]]}]
    with pytest.raises(ValueError,match='photo_core.*protected'):builder.build_editor(m,base/'bad',base)


def test_local_edit_cannot_resize_original_canvas(fixture):
    m,base=fixture;m['canvas_size']=[320,240]
    with pytest.raises(ValueError,match='original poster canvas'):builder.build_editor(m,base/'bad',base)


def test_same_aspect_uniform_mapping_has_no_margins(fixture):
    m,base=fixture
    Image.open(base/'template.png').resize((320,240),Image.Resampling.NEAREST).save(base/'scaled.png')
    m['template']='scaled.png'
    report=builder.build_editor(m,base/'ok',base)
    assert report['template_mapping']['offset']==[0,0]
    assert report['template_mapping']['mapped_size']==[160,120]
    assert Image.open(base/'ok/poster-template.png').getpixel((0,0))[3]==255
