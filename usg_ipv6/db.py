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


def _migrate_snapshots(conn: duckdb.DuckDBPyConnection) -> None:
    """Recreate snapshots with source_url and byte_count if the old schema lacks them."""
    try:
        cols = {
            row[0]
            for row in conn.execute("DESCRIBE snapshots").fetchall()
        }
    except Exception:
        return  # table doesn't exist yet — init_schema will create it

    if "source_url" in cols and "byte_count" in cols:
        return  # already up to date

    # Recreate with full schema, preserving existing rows.
    conn.execute("""
        CREATE TABLE _snapshots_new (
            snapshot_ts  TIMESTAMP NOT NULL,
            collector    VARCHAR   NOT NULL,
            source_url   VARCHAR   NOT NULL DEFAULT '',
            byte_count   BIGINT    NOT NULL DEFAULT 0,
            status       VARCHAR   NOT NULL,
            processed_at TIMESTAMP,
            PRIMARY KEY (snapshot_ts, collector)
        )
    """)
    conn.execute("""
        INSERT INTO _snapshots_new (snapshot_ts, collector, source_url, byte_count, status, processed_at)
        SELECT snapshot_ts, collector, '' AS source_url, 0 AS byte_count, status, processed_at
        FROM snapshots
    """)
    conn.execute("DROP TABLE snapshots")
    conn.execute("ALTER TABLE _snapshots_new RENAME TO snapshots")


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    _migrate_snapshots(conn)
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
    # ── Enrichment tables ─────────────────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nist_deploymon (
            measurement_date  DATE    NOT NULL,
            domain            VARCHAR NOT NULL,
            org               VARCHAR,
            service           VARCHAR NOT NULL,
            server_count      INTEGER NOT NULL DEFAULT 0,
            v6_addr_count     INTEGER NOT NULL DEFAULT 0,
            v6_reach_count    INTEGER NOT NULL DEFAULT 0,
            v6_func_count     INTEGER NOT NULL DEFAULT 0,
            status            VARCHAR,
            dnssec_signed     BOOLEAN,
            dnssec_valid      BOOLEAN,
            dnssec_chained    BOOLEAN,
            PRIMARY KEY (measurement_date, domain, service)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS domain_services (
            observation_date  DATE    NOT NULL,
            domain            VARCHAR NOT NULL,
            record_type       VARCHAR NOT NULL,
            hostname          VARCHAR NOT NULL,
            ipv6_address      VARCHAR NOT NULL,
            matched_prefix    VARCHAR,
            matched_asn       BIGINT,
            source            VARCHAR NOT NULL,
            PRIMARY KEY (observation_date, domain, record_type, hostname, ipv6_address)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rpki_validity (
            check_date        DATE    NOT NULL,
            prefix            VARCHAR NOT NULL,
            origin_asn        BIGINT  NOT NULL,
            status            VARCHAR NOT NULL,
            roa_max_length    INTEGER,
            PRIMARY KEY (check_date, prefix, origin_asn)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bgp_communities (
            snapshot_ts       TIMESTAMP NOT NULL,
            collector         VARCHAR   NOT NULL,
            prefix            VARCHAR   NOT NULL,
            origin_asn        BIGINT    NOT NULL,
            community         VARCHAR   NOT NULL,
            PRIMARY KEY (snapshot_ts, collector, prefix, origin_asn, community)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dnssec_status (
            check_date        DATE    NOT NULL,
            domain            VARCHAR NOT NULL,
            has_ds            BOOLEAN,
            has_dnskey        BOOLEAN,
            ad_validated      BOOLEAN,
            algorithms        VARCHAR,
            PRIMARY KEY (check_date, domain)
        )
    """)
    # ── RIPE Atlas enrichment tables ──────────────────────────────────────────
    conn.execute("""
        CREATE TABLE IF NOT EXISTS atlas_observations (
            observation_date  DATE    NOT NULL,
            target_address    VARCHAR NOT NULL,
            matched_prefix    VARCHAR NOT NULL,
            origin_asn        BIGINT  NOT NULL,
            measurement_type  VARCHAR NOT NULL,
            responsive        BOOLEAN NOT NULL,
            rtt_ms            DOUBLE,
            probe_count       INTEGER NOT NULL,
            responding_probes INTEGER NOT NULL DEFAULT 0,
            msm_id            BIGINT,
            PRIMARY KEY (observation_date, target_address, measurement_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS atlas_hop_addresses (
            observation_date  DATE    NOT NULL,
            hop_address       VARCHAR NOT NULL,
            matched_prefix    VARCHAR,
            origin_asn        BIGINT,
            probe_target      VARCHAR NOT NULL,
            msm_id            BIGINT,
            PRIMARY KEY (observation_date, hop_address, probe_target)
        )
    """)
