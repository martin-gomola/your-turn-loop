PORT ?= 8983

.PHONY: site dev check test
site:
	cp -R pets site/
dev: site
	python3 -m http.server $(PORT) --bind 127.0.0.1 --directory site
check:
	uv run --with-requirements requirements-dev.txt python scripts/validate.py
test: check
	uv run --with-requirements requirements-dev.txt python -m unittest discover -s tests -v
	uv run --with-requirements requirements-dev.txt python -m pytest skills/second-opinion/tests/test_run_advisor.py skills/delegate-code/tests/test_run_worker.py -v
	node --test tests/installer.test.mjs
