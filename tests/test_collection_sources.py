from hall.collection_sources import collection_source_plan


def test_collection_source_plan_uses_only_confirmed_routes():
    plan = collection_source_plan("キコーナ四条畷店")
    sources = {item["key"]: item for item in plan["sources"]}
    assert plan["hall_name"] == "キコーナ四條畷店"
    assert sources["anoslot"]["supported"] is True
    assert sources["anoslot"]["source_url"].endswith("/7964")
    assert sources["pworld"]["supported"] is True
    assert sources["dmm"]["supported"] is True
    assert sources["manual_seat"]["supported"] is True
    assert sources["manual_seat"]["automatic"] is False
    assert sources["minrepo_archive"]["supported"] is False


def test_collection_source_plan_exposes_archive_route_for_confirmed_store():
    plan = collection_source_plan("ニコニコ住道店")
    sources = {item["key"]: item for item in plan["sources"]}
    assert sources["minrepo_archive"]["supported"] is True
    assert sources["slomap_events"]["supported"] is True
    assert plan["summary"]["performance_source_count"] >= 2
