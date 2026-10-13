import duckdb


def get_connection(db_path: str) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(db_path)


def _migrate_ark_reachability(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop old 3-column ark_reachability so init_schema can recreate it."""
    try:
        cols = {
            row[0]
            for row in conn.execute("DESCRIBE ark_reachability").fetchall()
        }
        if "addresses_observed" not in cols:
            conn.execute("DROP TABLE ark_reachability")
    except Exception:
        pass  # table doesn't exist yet — init_schema will create it


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    _migrate_ark_reachability(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asn_corpus (
            asn             BIGINT PRIMARY KEY,
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
            snapshot_ts     TIMESTAMP NOT NULL,
            collector       VARCHAR NOT NULL,
            source_url      VARCHAR NOT NULL,
            byte_count      BIGINT NOT NULL,
            status          VARCHAR NOT NULL,
            processed_at    TIMESTAMP,
            PRIMARY KEY (snapshot_ts, collector)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prefix_records (
            snapshot_ts     TIMESTAMP NOT NULL,
            collector       VARCHAR NOT NULL,
            peer_asn        BIGINT NOT NULL,
            prefix          VARCHAR NOT NULL,
            origin_asn      BIGINT NOT NULL,
            prefix_len      INTEGER NOT NULL,
            af              INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS caida_pfx2as (
            snapshot_date   DATE NOT NULL,
            prefix          VARCHAR NOT NULL,
            prefix_len      INTEGER NOT NULL,
            asn             BIGINT NOT NULL,
            af              INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ark_reachability (
            measurement_date      DATE    NOT NULL,
            asn                   BIGINT  NOT NULL,
            ipv6_reachable        BOOLEAN NOT NULL,
            addresses_observed    INTEGER NOT NULL DEFAULT 0,
            prefixes_in_scope     INTEGER NOT NULL DEFAULT 0,
            pfx2as_snapshot_date  DATE    NOT NULL,
            asn_divergence_count  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (measurement_date, asn)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ark_prefix_observations (
            measurement_date      DATE    NOT NULL,
            prefix                VARCHAR NOT NULL,
            our_asn               BIGINT  NOT NULL,
            addresses_observed    INTEGER NOT NULL,
            addresses_ark_match   INTEGER NOT NULL,
            pfx2as_snapshot_date  DATE    NOT NULL,
            PRIMARY KEY (measurement_date, prefix)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hitlist_observations (
            observation_date  DATE    NOT NULL,
            asn               BIGINT  NOT NULL,
            responsive_count  INTEGER NOT NULL DEFAULT 0,
            aliased_count     INTEGER NOT NULL DEFAULT 0,
            hitlist_date      DATE    NOT NULL,
            PRIMARY KEY (observation_date, asn)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hitlist_addresses (
            observation_date  DATE    NOT NULL,
            address           VARCHAR NOT NULL,
            asn               BIGINT  NOT NULL,
            matched_prefix    VARCHAR NOT NULL,
            is_aliased        BOOLEAN NOT NULL DEFAULT FALSE,
            hitlist_date      DATE    NOT NULL,
            PRIMARY KEY (observation_date, address)
        )
    """)
