from __future__ import annotations

import copy
import hashlib
import importlib

import pytest


@pytest.fixture
def service():
    try:
        return importlib.import_module("app.services.material_menu_naming")
    except ModuleNotFoundError:
        pytest.fail("菜单命名服务尚未实现")


@pytest.fixture
def menu():
    return {
        "merchant_id": "keke", "menu_version": "test-v1",
        "items": [
            {"id": "keke-001", "zh": "香辣卤鸡（整只）", "en": "Spicy Braised Chicken"},
            {"id": "keke-002", "zh": "五香卤鸡（整只）", "en": "Five Spices Chicken"},
            {"id": "keke-027", "zh": "牛肚面", "en": "Beef Tripe Noodles"},
        ],
        "ambiguity_groups": [{"menu_ids": ["keke-001", "keke-002"], "reason": "整鸡味型需要人工确认"}],
    }


def payload(status="matched", selected="keke-027"):
    return {
        "naming_status": status,
        "selected_menu_id": selected,
        "candidate_menu_ids": [selected] if selected else [],
        "name_zh": "模型不能改菜单中文",
        "name_en": "Model Translation Must Be Ignored",
        "visual_subject": "牛肚面",
        "reason": "可见面条与条状牛肚，原文件名也一致。",
        "uncertain_attributes": [],
        "category": "食物",
        "tags": ["面食", "牛肚", "菜品特写"],
        "tag_groups": {"食材": ["面条", "牛肚"]},
    }


def test_matched_names_are_read_from_menu_verbatim(service, menu):
    result = service.validate_result(payload(), menu)
    assert result.name == "牛肚面_Beef Tripe Noodles"
    assert result.naming_status == "matched"
    assert result.selected_menu_id == "keke-027"


def test_chicken_flavour_cannot_be_auto_confirmed_from_model_confidence(service, menu):
    data = payload(selected="keke-001")
    data.update(visual_subject="整只卤鸡", reason="模型声称红色外皮就是香辣", confidence=1.0)
    result = service.validate_result(data, menu)
    assert result.naming_status == "ambiguous"
    assert result.selected_menu_id is None
    assert set(result.candidate_menu_ids) == {"keke-001", "keke-002"}
    assert result.name is None


def test_platter_outside_menu_uses_only_a_chinese_description(service, menu, tmp_path):
    data = payload("generated", None)
    data.update(name_zh="卤味拼盘", name_en="Braised Meat Platter", visual_subject="多种卤味拼盘")
    result = service.validate_result(data, menu)
    assert result.name == "卤味拼盘"
    assert result.naming_status == "generated"
    data_bytes = b'platter photo'
    target = service.write_named_copy(tmp_path, 'platter', result, suffix='.jpg', data=data_bytes, sha256=hashlib.sha256(data_bytes).hexdigest())
    assert target.name == '卤味拼盘.jpg'


def test_generated_chinese_description_does_not_require_english(service, menu):
    data = payload('generated', None)
    data.update(name_zh='卤味菜单价目牌', name_en=None)
    result = service.validate_result(data, menu)
    assert result.name == '卤味菜单价目牌'
    assert result.name_en is None
    prompt = service.build_messages(menu, 'placeholder')[0]['content']
    assert '菜单外只用中文描述名' in prompt


@pytest.mark.parametrize("changes", [
    {"selected_menu_id": "unknown"},
    {"candidate_menu_ids": ["unknown"]},
    {"category": "随便分类"},
    {"tag_groups": {"食材": ["beef"]}},
    {"naming_status": "invented"},
    {"reason": ""},
])
def test_invalid_model_results_do_not_become_named_files(service, menu, changes):
    data = payload()
    data.update(changes)
    with pytest.raises(service.MenuNamingError):
        service.validate_result(data, menu)


def test_menu_is_scoped_to_merchant_and_rejects_duplicate_ids(service, menu):
    with pytest.raises(service.MenuNamingError):
        service.validate_menu(menu, merchant_id="another-merchant")
    duplicate = copy.deepcopy(menu)
    duplicate["items"].append(dict(duplicate["items"][0]))
    with pytest.raises(service.MenuNamingError):
        service.validate_menu(duplicate)


def test_prompt_has_no_content_filters_and_one_explicit_image_call(service, menu):
    class Client:
        def __init__(self):
            self.calls = []

        def chat_json(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return payload()

    client = Client()
    result = service.classify_image(client, menu=menu, image_data_url="data:image/jpeg;base64,stub", original_name="牛肚面.JPG", model="deepseek-v4-flash", timeout_seconds=90)
    messages, options = client.calls[0]
    assert len(client.calls) == 1
    assert options == {"model": "deepseek-v4-flash", "timeout_seconds": 90}
    assert "不做空座位、螃蟹腿" in messages[0]["content"]
    assert "keke-027" in messages[0]["content"]
    assert messages[1]["content"][1]["image_url"]["url"].endswith(",stub")
    assert result.name == "牛肚面_Beef Tripe Noodles"


def test_identical_names_keep_both_original_files_without_numbers(service, menu, tmp_path):
    result = service.validate_result(payload(), menu)
    a, b = b"original-file-a", b"original-file-b"
    p1 = service.write_named_copy(tmp_path, "photo_a", result, suffix=".JPG", data=a, sha256=hashlib.sha256(a).hexdigest())
    p2 = service.write_named_copy(tmp_path, "photo_b", result, suffix=".JPG", data=b, sha256=hashlib.sha256(b).hexdigest())
    assert p1.name == p2.name == "牛肚面_Beef Tripe Noodles.jpg"
    assert p1.parent != p2.parent
    assert p1.read_bytes() == a and p2.read_bytes() == b
    with pytest.raises(service.MenuNamingError):
        service.write_named_copy(tmp_path, "photo_a", result, suffix=".jpg", data=b, sha256=hashlib.sha256(b).hexdigest())


def test_export_rejects_traversal_and_ambiguous_names_before_writing(service, menu, tmp_path):
    data = b"bytes"
    result = service.validate_result(payload(), menu)
    for photo_id in ["../escape", "\\\\server\\share", "a/b"]:
        with pytest.raises(service.MenuNamingError):
            service.write_named_copy(tmp_path, photo_id, result, suffix=".jpg", data=data, sha256=hashlib.sha256(data).hexdigest())
    uncertain = service.validate_result(payload(selected="keke-001"), menu)
    with pytest.raises(service.MenuNamingError):
        service.write_named_copy(tmp_path, "uncertain", uncertain, suffix=".jpg", data=data, sha256=hashlib.sha256(data).hexdigest())
    assert not list(tmp_path.iterdir())


def test_native_scheme_keeps_menu_and_does_not_restore_seejoy_filters(service, menu, tmp_path):
    import mongomock
    from app.config import Settings
    from app.services import material_filters as store
    from app.services.scoped_db import ScopedDatabase

    db = ScopedDatabase(mongomock.MongoClient().db, "keke")
    settings = Settings(storage_dir=str(tmp_path / "store"))
    folder, _ = store.add_folder(db, r"\\server\share\photos", readable_check=lambda _:None)
    scheme = store.create_scheme(db, settings, folder, name="菜单命名", naming_mode="menu_match", menu=menu)
    assert scheme["rules"] == []
    assert scheme["menu"]["menu_version"] == "test-v1"
    clone = store.clone_scheme(db, settings, scheme, name="副本")
    assert clone["naming_mode"] == "menu_match" and clone["rules"] == []
    assert clone["menu"] == scheme["menu"]


def test_native_runner_saves_duplicate_bilingual_names_and_skips_completed(service, menu, tmp_path):
    import io
    import mongomock
    from PIL import Image
    from app.config import Settings
    from app.services import material_filters as store
    from app.services import material_filter_runner as runner
    from app.services.scoped_db import ScopedDatabase

    source = tmp_path / "source"
    source.mkdir()
    for name, color in [("a.jpg", "red"), ("b.jpg", "blue")]:
        buf=io.BytesIO()
        Image.new("RGB", (48,32), color).save(buf,format="JPEG")
        (source/name).write_bytes(buf.getvalue())
    before={p.name: (p.stat().st_mtime_ns,p.read_bytes()) for p in source.iterdir()}
    db=ScopedDatabase(mongomock.MongoClient().db,"keke")
    db.material_filter_folders.insert_one({"_id":"folder", "source_root":str(source), "canonical_source_key":"local-test"})
    settings=Settings(storage_dir=str(tmp_path/"store"), core_ai_api_key="test-key")
    scheme=store.create_scheme(db,settings,store.get_folder(db,"folder"),name="菜单试跑",naming_mode="menu_match",menu=menu)
    class Client:
        calls=0
        def chat_json(self,*args,**kwargs):
            self.calls+=1
            return payload()
    client=Client()
    for request in ["first","again"]:
        run,_=store.create_run(db,scheme,request_id=request)
        result=runner.run_scan(db,settings,client,run["_id"])
        assert result["status"]=="succeeded"
    assert client.calls==2
    rows=list(db.material_filter_evaluations.find())
    assert len(rows)==2
    for row in rows:
        assert row["display_name"]=="牛肚面_Beef Tripe Noodles"
        assert row["output_name"]=="牛肚面_Beef Tripe Noodles.jpg"
        assert row["naming"]["selected_menu_id"]=="keke-027"
        assert store.output_file(row,settings,scheme).read_bytes()==before[row["original_name"]][1]
    assert rows[0]["output_path"]!=rows[1]["output_path"]
    reviewed=store.review_evaluation(db,settings,scheme,rows[0],decision="pass",source_sha256=rows[0]["source_sha256"],request_id="confirm-menu")
    assert reviewed["output_name"]=="牛肚面_Beef Tripe Noodles.jpg"
    for row in rows:
        store.set_manual_pick(db,row,picked=True)
    exported=store.export_picked(db,settings,scheme)
    from pathlib import Path
    exported_files=list(Path(exported["directory"]).rglob("*.jpg"))
    assert len(exported_files)==2
    assert len({p.name for p in exported_files})==1
    assert before=={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in source.iterdir()}

    picked=store.get_evaluation(db,rows[0]["_id"])
    picked=store.update_evaluation(db,picked["_id"],{"model":"old-model","prompt_version":"old-prompt"})
    class AmbiguousClient:
        def chat_json(self,*args,**kwargs):
            data=payload("ambiguous",None)
            data["candidate_menu_ids"]=["keke-001","keke-002"]
            data["visual_subject"]="卤鸡"
            return data
    changed=runner.recheck_evaluation(db,settings,AmbiguousClient(),scheme,picked)
    assert changed["model"]==scheme["model"]
    assert changed["prompt_version"]==scheme["prompt_version"]
    assert changed["naming"]["naming_status"]=="ambiguous"
    assert changed["effective_decision"]=="uncertain"
    assert changed["copy_status"]=="none" and changed["output_path"] is None
    assert changed["manual_pick"] is False


def test_menu_sync_names_do_not_add_numbers_on_collisions():
    from app.services.material_filter_uat_sync import _planned_name
    row={"naming_mode":"menu_match", "display_name":"牛肚面_Beef Tripe Noodles"}
    used={"牛肚面_beef tripe noodles.jpg"}
    assert _planned_name(row,".jpg",used)=="牛肚面_Beef Tripe Noodles.jpg"
    assert _planned_name(row,".jpg",set(),"卤牛腱肉_Spiced Beef")=="卤牛腱肉_Spiced Beef.jpg"
    assert _planned_name({"naming_mode":"shared","display_name":"A"},".jpg",set(),"卤牛腱肉_Spiced Beef")=="A.jpg"


@pytest.fixture
def english_menu(menu):
    value=copy.deepcopy(menu)
    value["name_format"]="en"
    for item in value["items"]:
        item["zh"]=None
    return value


def test_english_menu_preserves_null_chinese_and_exact_english_filename(service,english_menu,tmp_path):
    result=service.validate_result(payload(),english_menu)
    assert result.name_zh is None
    assert result.name=="Beef Tripe Noodles"
    data=b"original bytes"
    path=service.write_named_copy(tmp_path,"english_photo",result,suffix=".JPG",data=data,sha256=hashlib.sha256(data).hexdigest())
    assert path.name=="Beef Tripe Noodles.jpg"


def test_menu_choice_slashes_preserve_original_names_and_write_one_safe_file(service, english_menu, tmp_path):
    english_menu['items'][2].update(zh='孜然牛/羊', en='Beef / Lamb')
    validated = service.validate_menu(english_menu)
    assert validated['items'][2]['zh'] == '孜然牛/羊'
    assert validated['items'][2]['en'] == 'Beef / Lamb'
    result = service.validate_result(payload(), validated)
    assert result.name == 'Beef / Lamb'
    data = b'original photo'
    target = service.write_named_copy(tmp_path, 'choice', result, suffix='.jpg', data=data, sha256=hashlib.sha256(data).hexdigest())
    assert target.parent == tmp_path / 'choice'
    assert target.name == 'Beef ／ Lamb.jpg'
    assert target.read_bytes() == data


def test_full_szechuan_menu_is_valid_and_keeps_user_pairs(service):
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'data' / 'menus'
    raw = json.loads((root / 'szechuan-gourmet-full-20261112.raw.json').read_text(encoding='utf-8'))
    menu = json.loads((root / 'szechuan-gourmet-full-20261112.json').read_text(encoding='utf-8'))
    validated = service.validate_menu(menu, merchant_id='ec3a8e03-28c0-4291-9f94-14289e63cdc7')
    assert len(raw) == 177
    assert len(validated['items']) == 162
    assert {(i['zh'], i['en']) for i in validated['items']} == {(i['zh'], i['en']) for i in raw}
    assert validated['name_format'] == 'zh_en'
    prompt = service.build_messages(validated, 'placeholder')[0]['content']
    assert 'Spinach & Shredded Chicken Soup' in prompt
    assert 'Your Choice of Lo Mein' in prompt


def test_generated_names_use_chinese_even_with_legacy_english_menu(service,english_menu):
    data=payload("generated",None)
    data["name_zh"]="荤素拼盘"
    data["name_en"]="Mixed Dish Platter"
    result=service.validate_result(data,english_menu)
    assert result.name=="荤素拼盘" and result.name_en is None
    prompt=service.build_messages(english_menu,"data:image/jpeg;base64,stub")[0]["content"]
    assert "非菜品只用中文" in prompt


def test_nonfood_portrait_uses_chinese_and_keeps_chinese_staff_tags(service, english_menu):
    data = payload("generated", None)
    data.update(category="人物", name_zh="黑衬衫棕围裙男子肖像", name_en="Portrait of a Man",
                tag_groups={"员工": ["围裙", "人物肖像"]})
    result = service.validate_result(data, english_menu)
    assert result.name == "黑衬衫棕围裙男子肖像"
    assert result.name_en is None
    assert result.tag_groups == {"员工": ["围裙", "人物肖像"]}
    assert result.tags == ["围裙", "人物肖像"]


def test_photo_can_have_multiple_chinese_tag_groups(service, menu):
    data = payload()
    data["tag_groups"] = {"食材": ["牛肚", "面条", "牛肚"], "员工": ["端菜"], "环境": ["吧台"]}
    result = service.validate_result(data, menu)
    assert result.tag_groups == {"食材": ["牛肚", "面条"], "员工": ["端菜"], "环境": ["吧台"]}
    assert result.tags == ["牛肚", "面条", "端菜", "吧台"]
    assert service.as_classification(result).naming["tag_groups"] == result.tag_groups


def test_persisted_legacy_prompt_receives_current_tag_protocol_once(service, menu):
    legacy = "用户原有的识别要求。tags为3–6个标签。"
    prompt = service.build_messages(menu, "placeholder", system_prompt=legacy)[0]["content"]
    assert prompt.startswith(legacy)
    assert "tag_groups" in prompt
    assert "禁止英文标签" in prompt
    current = service.build_messages(menu, "placeholder")[0]["content"]
    assert service.build_messages(menu, "placeholder", system_prompt=current)[0]["content"] == current


@pytest.mark.parametrize("tag_groups", [None, {}, {"品牌": ["门头"]}, {"食材": ["beef"]},
                                         {"员工": ["服务员waiter"]}, {"环境": ["123"]}, {"食材": "牛肉"}])
def test_invalid_or_nonchinese_grouped_tags_are_rejected(service, menu, tag_groups):
    data = payload()
    data["tag_groups"] = tag_groups
    with pytest.raises(service.MenuNamingError):
        service.validate_result(data, menu)


def test_uncertain_dish_keeps_chinese_subject_without_a_menu_name(service, menu):
    data = payload("ambiguous", None)
    data.update(candidate_menu_ids=["keke-001", "keke-002"], visual_subject="炒肉片配黑木耳")
    result = service.validate_result(data, menu)
    classified = service.as_classification(result)
    assert result.name is None
    assert classified.name == "炒肉片配黑木耳（菜名待确认）"
    assert classified.decision == "uncertain"


@pytest.mark.parametrize("unknown", [["馅料种类", "份量"], ["肉类种类无法确认"], ["口味无法确认"]])
def test_match_with_unresolved_dish_attributes_must_remain_uncertain(service, menu, unknown):
    data = payload()
    data["uncertain_attributes"] = unknown
    result = service.validate_result(data, menu)
    assert result.naming_status == "ambiguous"
    assert result.selected_menu_id is None
    assert result.candidate_menu_ids == ["keke-027"]
    assert result.name is None
    assert service.as_classification(result).decision == "uncertain"


def test_null_chinese_requires_explicit_english_mode(service,english_menu):
    del english_menu["name_format"]
    with pytest.raises(service.MenuNamingError):
        service.validate_menu(english_menu)


def test_english_ambiguity_still_does_not_create_a_formal_name(service,english_menu):
    result=service.validate_result(payload(selected="keke-001"),english_menu)
    assert result.naming_status=="ambiguous" and result.name is None


def test_recursive_trial_samples_different_folders_instead_of_one_continuous_burst():
    from scripts.trial_menu_naming import select_five_recursive
    from app.services.material_filter_files import SourceEntry
    entries=[]
    for group in range(6):
        for index in range(10-group):
            relative=f"folder-{group}/photos/{index}.jpg"
            entries.append(SourceEntry(relative,str(index)+".jpg",1,1,relative))
    selected=select_five_recursive(entries)
    assert len(selected)==5
    assert len({e.relative_path.split('/')[0] for e in selected})==5
