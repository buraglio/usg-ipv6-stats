.PHONY: setup corpus run backfill test lint

DB_PATH ?= data/usg_ipv6.duckdb
COLLECTOR ?= route-views2
SINCE ?= 2021-01-01
UNTIL ?= 2024-12-31
AS2ORG ?= data/as2org.txt.gz

setup:
	uv sync

corpus:
	uv run python -m usg_ipv6.corpus.build \
		--db $(DB_PATH) \
		--as2org $(AS2ORG) \
		--version $(shell date +%Y-%m-%d)

run:
	uv run python -c "from usg_ipv6.pipeline.runner import run_incremental; \
		import json; print(json.dumps(run_incremental('$(DB_PATH)', '$(COLLECTOR)', limit=1)))"

backfill:
	uv run python -c "from usg_ipv6.pipeline.runner import run_backfill; \
		from datetime import datetime; import json; \
		print(json.dumps(run_backfill('$(DB_PATH)', '$(COLLECTOR)', \
		datetime.fromisoformat('$(SINCE)'), datetime.fromisoformat('$(UNTIL)'))))"

test:
	uv run pytest

lint:
	uv run python -m py_compile usg_ipv6/db.py \
		usg_ipv6/corpus/federal_agencies.py \
		usg_ipv6/corpus/arin.py \
		usg_ipv6/corpus/caida.py \
		usg_ipv6/corpus/schema.py \
		usg_ipv6/corpus/build.py \
		usg_ipv6/pipeline/routeviews.py \
		usg_ipv6/pipeline/parse.py \
		usg_ipv6/pipeline/normalize.py \
		usg_ipv6/pipeline/caida_pfx2as.py \
		usg_ipv6/pipeline/runner.py
