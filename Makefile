.PHONY: test

test:
	@set -e; created=0; if [ ! -f config/config.jsonc ]; then cp config/config.example.jsonc config/config.jsonc; created=1; fi; trap 'if [ "$$created" = 1 ]; then rm -f config/config.jsonc; fi' EXIT; uv sync --frozen --extra dev; uv run --frozen --extra dev pytest tests/unit -q
