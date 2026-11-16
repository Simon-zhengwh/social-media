import hashlib
import base64
import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest
from PIL import Image

SCRIPTS=Path(__file__).parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
SPEC=importlib.util.spec_from_file_location('dish_builder',SCRIPTS/'build_dish_editor.py')
dishes=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dishes)


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def approve_synthetic_scene(manifest, base, directory='scene-preview'):
    preview = base / directory
    dishes.prepare_background(manifest, preview, base)
    review_path = preview / 'background-review.json'
    review = json.loads(review_path.read_text())
    review['status'] = 'PASS'
    review['notes'] = 'Synthetic solid-color scene fixture with explicitly defined repair polygon.'
    (base / 'background-review.json').write_text(json.dumps(review))


@pytest.fixture
def case(tmp_path):
    Image.new('RGB',(160,120),'#3c2818').save(tmp_path/'poster.png')
    Image.new('RGB',(160,120),'#402b19').save(tmp_path/'background.png')
    Image.new('RGB',(40,40),'#b87820').save(tmp_path/'source.png')
    source=Image.open(tmp_path/'source.png').convert('RGBA');alpha=Image.new('L',(40,40));alpha.paste(255,(5,5,35,35));source.putalpha(alpha);source.save(tmp_path/'cutout.png')
    (tmp_path/'segmentation.json').write_text(json.dumps({'method':'grabcut_cpu','source_sha256':digest(tmp_path/'source.png'),'rgb_preserved':True}))
    review={'status':'PASS','background_sha256':digest(tmp_path/'background.png'),'notes':'Synthetic uniform background fixture; no actual food content exists.'}
    (tmp_path/'background-review.json').write_text(json.dumps(review))
    (tmp_path/'cutout-review.json').write_text(json.dumps({'status':'PASS','cutout_sha256':digest(tmp_path/'cutout.png'),'notes':'Synthetic square subject; exact expected mask.'}))
    m={'mode':'dishes','poster':'poster.png','background':'background.png','repair_region':{'polygon':[[40,30],[120,30],[120,100],[40,100]]},'background_review':'background-review.json','dishes':[{'id':'dish-one','original':'source.png','cutout':'cutout.png','segmentation_report':'segmentation.json','cutout_review':'cutout-review.json','box':[30,30,100,100]}]}
    approve_synthetic_scene(m, tmp_path)
    return m,tmp_path


def test_dishes_are_free_layers_over_opaque_background_and_replay(case):
    m,base=case;out=base/'out'
    r=dishes.build_dish_editor(m,out,base)
    assert r['html_sha256'] == digest(out/'editor.html')
    assert r['mode']=='dishes' and r['old_shape_clips_subject'] is False
    payload=json.loads(re.search(r'<script id="editor-data" type="application/json">(.*?)</script>',(out/'editor.html').read_text(encoding='utf-8'),re.S).group(1))
    assert payload['layers'][0]['kind']=='dish' and 'window' not in payload['layers'][0]
    clean=Image.open(out/'clean-background.png').convert('RGBA')
    assert clean.getchannel('A').getextrema()==(255,255)
    assert clean.getpixel((0,0))==Image.open(base/'poster.png').convert('RGBA').getpixel((0,0))
    dishes.build_dish_editor(out/'manifest.json',base/'replay')
    assert Image.open(base/'replay/clean-background.png').tobytes()==clean.convert('RGB').tobytes()


def test_each_dish_embeds_original_and_cutout_with_cutout_default(case):
    m, base = case
    m['dishes'].append({**m['dishes'][0], 'id': 'dish-two', 'box': [80, 40, 145, 110]})
    dishes.build_dish_editor(m, base / 'out', base)
    def payload(folder):
        html = (folder / 'editor.html').read_text(encoding='utf-8')
        return json.loads(re.search(r'<script id="editor-data" type="application/json">(.*?)</script>', html, re.S).group(1))
    data = payload(base / 'out')
    for layer in data['layers']:
        assert layer['backgroundMode'] == 'cutout'
        assert base64.b64decode(layer['originalSource'].split(',', 1)[1]) == (base / 'source.png').read_bytes()
        assert base64.b64decode(layer['source'].split(',', 1)[1]) == (base / 'cutout.png').read_bytes()
    dishes.build_dish_editor(base / 'out/manifest.json', base / 'replay')
    assert payload(base / 'replay') == data


def test_known_bad_visual_review_blocks_delivery(case):
    m,base=case
    review=json.loads((base/'background-review.json').read_text());review['status']='FAIL';(base/'background-review.json').write_text(json.dumps(review))
    with pytest.raises(ValueError,match='review'):dishes.build_dish_editor(m,base/'out',base)
    assert not (base/'out/editor.html').exists()


def test_subject_rgb_cannot_be_repainted(case):
    m,base=case
    im=Image.open(base/'cutout.png');im.putpixel((10,10),(0,255,0,255));im.save(base/'cutout.png')
    with pytest.raises(ValueError,match='RGB'):dishes.build_dish_editor(m,base/'out',base)


def test_full_opacity_and_exact_background_frame_are_required(case):
    m,base=case
    Image.new('RGBA',(160,120),(20,20,20,252)).save(base/'background.png')
    with pytest.raises(ValueError,match='opaque'):dishes.build_dish_editor(m,base/'out',base)
    Image.new('RGB',(150,120),'brown').save(base/'background.png')
    with pytest.raises(ValueError,match='canvas'):dishes.build_dish_editor(m,base/'out',base)


def test_multiple_dishes_and_foreground_preservation(case):
    m,base=case;m['dishes'].append({**m['dishes'][0],'id':'dish-two','box':[80,40,145,110]})
    m['protected_regions']=[{'id':'price','polygon':[[90,70],[110,70],[110,90],[90,90]]}]
    approve_synthetic_scene(m, base, 'protected-preview')
    r=dishes.build_dish_editor(m,base/'out',base)
    assert r['dish_count']==2 and r['outside_repair_changed_pixels']==0
    assert Image.open(base/'out/fixed-foreground.png').getpixel((100,80))==Image.open(base/'poster.png').convert('RGBA').getpixel((100,80))


def test_image_review_must_match_actual_files(case):
    m,base=case
    Image.new('RGB',(160,120),'#442a1a').save(base/'background.png')
    with pytest.raises(ValueError,match='review.*hash'):dishes.build_dish_editor(m,base/'out',base)


def test_review_must_cover_final_scene_after_mask_composition(case):
    m,base=case
    # A previously approved model image cannot approve a different repair mask.
    m['repair_region']={'polygon':[[50,40],[110,40],[110,90],[50,90]]}
    with pytest.raises(ValueError,match='scene.*hash'):
        dishes.build_dish_editor(m,base/'out',base)
    assert not (base/'out/editor.html').exists()


def test_prepared_scene_stays_pending_until_review(case):
    manifest, base = case
    preview = base / 'pending-preview'
    result = dishes.prepare_background(manifest, preview, base)
    assert result['status'] == 'PENDING_visual_review'
    assert not (preview / 'editor.html').exists()
    manifest['background_review'] = 'pending-preview/background-review.json'
    with pytest.raises(ValueError, match='review has not passed'):
        dishes.build_dish_editor(manifest, base / 'out', base)


def test_cpu_grabcut_preserves_original_rgb(tmp_path):
    import numpy as np
    from PIL import ImageDraw
    from segment_photo import segment_photo
    original=Image.new('RGB',(128,128),(65,65,65));draw=ImageDraw.Draw(original)
    draw.ellipse((15,30,112,110),fill=(235,235,235));draw.ellipse((38,20,90,90),fill=(180,105,35))
    path=tmp_path/'dish.png';original.save(path)
    report=segment_photo({'image':str(path),'box':[8,10,120,120],'foreground':[[60,60],[30,70]],'background':[[2,2],[125,125]],'min_component_area':16},tmp_path/'cut')
    cut=np.array(Image.open(tmp_path/'cut/cutout.png'))
    assert report['method']=='grabcut_cpu' and report['image_generation_calls']==0
    assert np.array_equal(cut[:,:,:3],np.array(original))
    assert cut[60,60,3]==255 and cut[2,2,3]==0


def test_registration_aligns_only_stable_regions(tmp_path):
    import numpy as np
    import cv2
    from register_background import register_background
    rng=np.random.default_rng(17)
    texture=cv2.GaussianBlur(rng.integers(0,256,(400,480,3),dtype=np.uint8),(3,3),0)
    Image.fromarray(texture).save(tmp_path/'poster.png')
    excluded=Image.new('L',(480,400));excluded.paste(255,(140,140,330,280));excluded.save(tmp_path/'exclude.png')
    repaired=texture.copy();repaired[140:280,140:330]=[70,40,20]
    Image.fromarray(repaired).resize((400,440)).save(tmp_path/'generated.png')
    r=register_background(tmp_path/'poster.png',tmp_path/'generated.png',tmp_path/'exclude.png',tmp_path/'registered')
    assert r['repair_coverage_complete'] and r['inliers']>=20
    assert Image.open(tmp_path/'registered/aligned-background.png').size==(480,400)
