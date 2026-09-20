from hall.names import canonical_hall_name, hall_names_match, normalize_hall_key
from hall.regions import region_matches


def test_confirmed_shijonawate_aliases_share_canonical_name():
    assert canonical_hall_name("キコーナ四条畷店") == "キコーナ四條畷店"
    assert canonical_hall_name("SUPER COSMO PREMIUM 大東店") == "スーパーコスモプレミアム大東店"
    assert hall_names_match("ひまわり四條畷店", "ひま・わり四條畷店")
    assert region_matches("キコーナ四条畷店", "大阪府", "shijonawate")


def test_unknown_hall_name_is_not_aggressively_rewritten():
    assert canonical_hall_name("未登録テストホール") == "未登録テストホール"
    assert normalize_hall_key("ラッシュMATSUMOTO#59") == normalize_hall_key("ラッシュ松本#59")
