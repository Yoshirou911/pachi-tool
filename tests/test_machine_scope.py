from hall.machine_scope import is_smartslot_machine


def test_accepts_common_smartslot_labels():
    assert is_smartslot_machine("スマスロ北斗の拳")
    assert is_smartslot_machine("L東京喰種")
    assert is_smartslot_machine("LBパチスロ ヱヴァンゲリヲン")
    assert is_smartslot_machine("真打 吉宗")
    assert is_smartslot_machine("いざ！番長")


def test_rejects_juggler_and_non_target_rows():
    assert not is_smartslot_machine("マイジャグラーV")
    assert not is_smartslot_machine("パチスロ甲鉄城のカバネリ")
    assert not is_smartslot_machine("_NODATA_")
    assert not is_smartslot_machine("")
