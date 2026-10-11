import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from datetime import datetime
from usg_ipv6.pipeline.normalize import load_parquet


def _write_parquet(path, rows):
    schema = pa.schema([
        pa.field("prefix", pa.string()),
        pa.field("origin_asn", pa.int32()),
        pa.field("prefix_len", pa.int16()),
        pa.field("af", pa.int8()),
    ])
    table = pa.table(
        {k: [r[k] for r in rows] for k in schema.names},
        schema=schema,
    )
    pq.write_table(table, path)


def test_load_parquet_filters_to_corpus_asns(tmp_path, db):
    db.execute(
        "INSERT INTO asn_corpus VALUES (396260,'GSA','GSA','cfo_act','USGSA','high','arin','2026-10-11','2026-10-11')"
    )
    pq_path = str(tmp_path / "test.parquet")
    _write_parquet(pq_path, [
        {"prefix": "192.0.2.0/24", "origin_asn": 396260, "prefix_len": 24, "af": 4},
        {"prefix": "10.0.0.0/8",   "origin_asn": 99999,  "prefix_len": 8,  "af": 4},
    ])
    ts = datetime(2024, 3, 15, 8, 0)
    db.execute(
        "INSERT INTO snapshots VALUES (?, 'route-views2', 'http://x', 1000, 'downloaded', NULL)",
        [ts],
    )
    count = load_parquet(pq_path, ts, "route-views2", db)
    assert count == 1
    rows = db.execute("SELECT origin_asn FROM prefix_records").fetchall()
    assert rows == [(396260,)]


def test_load_parquet_marks_snapshot_processed(tmp_path, db):
    db.execute(
        "INSERT INTO asn_corpus VALUES (396260,'GSA','GSA','cfo_act','USGSA','high','arin','2026-10-11','2026-10-11')"
    )
    pq_path = str(tmp_path / "empty.parquet")
    _write_parquet(pq_path, [])
    ts = datetime(2024, 3, 15, 10, 0)
    db.execute(
        "INSERT INTO snapshots VALUES (?, 'route-views2', 'http://y', 1000, 'downloaded', NULL)",
        [ts],
    )
    load_parquet(pq_path, ts, "route-views2", db)
    row = db.execute("SELECT status FROM snapshots WHERE snapshot_ts = ?", [ts]).fetchone()
    assert row[0] == "processed"
