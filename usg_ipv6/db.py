import duckdb


def get_connection(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path)


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asn_corpus (
            asn             INTEGER PRIMARY KEY,
            org_name        VARCHAR NOT NULL,
            agency          VARCHAR NOT NULL,
            tier            VARCHAR NOT NULL,
            arin_org_handle VARCHAR,
            confidence      VARCHAR NOT NULL,
            sources         VARCHAR NOT NULL,
            last_verified   DATE NOT NULL,
            corpus_version  VARCHAR NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_ts     TIMESTAMP PRIMARY KEY,
            collector       VARCHAR NOT NULL,
            source_url      VARCHAR NOT NULL,
            byte_count      BIGINT NOT NULL,
            status          VARCHAR NOT NULL,
            processed_at    TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prefix_records (
            snapshot_ts     TIMESTAMP NOT NULL,
            collector       VARCHAR NOT NULL,
            peer_asn        INTEGER NOT NULL,
            prefix          VARCHAR NOT NULL,
            origin_asn      INTEGER NOT NULL,
            prefix_len      INTEGER NOT NULL,
            af              INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS caida_pfx2as (
            snapshot_date   DATE NOT NULL,
            prefix          VARCHAR NOT NULL,
            prefix_len      INTEGER NOT NULL,
            asn             INTEGER NOT NULL,
            af              INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ark_reachability (
            measurement_date DATE NOT NULL,
            asn              INTEGER NOT NULL,
            ipv6_reachable   BOOLEAN NOT NULL
        )
    """)
