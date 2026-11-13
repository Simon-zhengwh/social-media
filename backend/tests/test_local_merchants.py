from types import SimpleNamespace

from app.local_merchants import (
    local_merchant_catalog,
    local_merchant_name,
    parse_merchant_entries,
)


def _settings(**overrides):
    values = {
        "am_bypass_merchant_id": "local-merchant",
        "am_bypass_merchant_name": "SEE JOY喜聚",
        "am_bypass_merchants": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_parse_entries_accepts_semicolons_newlines_and_spacing():
    raw = " a=Alpha ; b = Beta \n c=Gamma;=NoId;d"
    assert parse_merchant_entries(raw) == [
        {"id": "a", "name": "Alpha"},
        {"id": "b", "name": "Beta"},
        {"id": "c", "name": "Gamma"},
        {"id": "d", "name": ""},
    ]


def test_catalog_puts_the_primary_merchant_first_and_drops_duplicates():
    settings = _settings(
        am_bypass_merchants=" local-merchant=Ignored;keke=Keke Food可可小卤 "
    )
    assert local_merchant_catalog(settings) == [
        {"id": "local-merchant", "name": "SEE JOY喜聚"},
        {"id": "keke", "name": "Keke Food可可小卤"},
    ]


def test_catalog_falls_back_to_the_id_when_a_name_is_missing():
    assert local_merchant_catalog(_settings(am_bypass_merchants="keke"))[1] == {
        "id": "keke",
        "name": "keke",
    }


def test_catalog_is_empty_without_a_primary_merchant():
    assert local_merchant_catalog(_settings(am_bypass_merchant_id="")) == []


def test_name_lookup_uses_the_entry_then_the_configured_default():
    settings = _settings(am_bypass_merchants="keke=Keke Food可可小卤")
    assert local_merchant_name(settings, "keke") == "Keke Food可可小卤"
    assert local_merchant_name(settings, "unknown") == "SEE JOY喜聚"
    assert local_merchant_name(_settings(am_bypass_merchant_name=""), "unknown") == "本地商户"
