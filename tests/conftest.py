import pytest
from usg_ipv6.db import get_connection, init_schema


@pytest.fixture
def db(tmp_path):
    conn = get_connection(str(tmp_path / "test.duckdb"))
    init_schema(conn)
    return conn
