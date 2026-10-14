import duckdb
import pyarrow.parquet as pq
from datetime import datetime


def load_parquet(
    parquet_path: str,
    snapshot_ts: datetime,
    collector: str,
    conn: duckdb.DuckDBPyConnection,
) -> int:
    corpus_asns = {
        row[0] for row in conn.execute("SELECT asn FROM asn_corpus").fetchall()
    }
    table = pq.read_table(parquet_path)
    df = table.to_pydict()

    # Use a set to deduplicate: MRT RIBs contain one entry per peer per prefix,
    # so the same (prefix, origin_asn) appears once per collector peer (~40x).
    rows_to_insert = list({
        (snapshot_ts, collector, 0, df["prefix"][i], df["origin_asn"][i],
         int(df["prefix_len"][i]), int(df["af"][i]))
        for i in range(len(df["prefix"]))
        if df["origin_asn"][i] in corpus_asns
    })

    if rows_to_insert:
        conn.executemany(
            "INSERT INTO prefix_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows_to_insert,
        )

    conn.execute(
        "UPDATE snapshots SET status = 'processed', processed_at = NOW() "
        "WHERE snapshot_ts = ? AND collector = ?",
        [snapshot_ts, collector],
    )

    return len(rows_to_insert)
