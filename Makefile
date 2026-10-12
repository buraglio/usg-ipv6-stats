.PHONY: setup corpus run backfill ark ark-backfill test lint

DB_PATH ?= data/usg_ipv6.duckdb
COLLECTOR ?= route-views2
SINCE ?= 2021-01-01
UNTIL ?= 2024-12-31
AS2ORG ?=

setup:
	uv sync

corpus:
	uv run python -m usg_ipv6.corpus.build \
		--db $(DB_PATH) \
		$(if $(AS2ORG),--as2org $(AS2ORG),) \
		--version $(shell date +%Y-%m-%d)

run:
	uv run usg run ingest --db $(DB_PATH) --collector $(COLLECTOR) --limit 1

backfill:
	uv run usg run backfill $(SINCE) $(UNTIL) --db $(DB_PATH) --collector $(COLLECTOR)

ark:
	uv run usg ark ingest --db $(DB_PATH)

ark-backfill:
	uv run usg ark backfill $(SINCE) $(UNTIL) --db $(DB_PATH)

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
		usg_ipv6/pipeline/runner.py \
		usg_ipv6/pipeline/lpm.py \
		usg_ipv6/pipeline/ark.py \
		usg_ipv6/cli.py \
		usg_ipv6/report_html.py
