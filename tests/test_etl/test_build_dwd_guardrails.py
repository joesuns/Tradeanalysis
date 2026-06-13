import logging

from backend.db.schema import create_all_tables


def test_rebuild_all_dwd_warns_on_large_subset(temp_db, caplog):
    from backend.etl.build_dwd import rebuild_all_dwd

    create_all_tables(temp_db)
    codes = ["{:06d}.SZ".format(i) for i in range(501)]
    with caplog.at_level(logging.WARNING):
        rebuild_all_dwd(temp_db, codes)
    assert any("large subset rebuild" in r.message for r in caplog.records)
