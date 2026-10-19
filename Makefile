PYTHON ?= python3

.PHONY: test lint format integration install uninstall clean

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff check --fix .

# Only in a throwaway root container/VM: starts its own NetworkManager.
integration:
	sh tests/integration/sandbox.sh start
	NETBIRD_NM_INTEGRATION=1 $(PYTHON) -m pytest tests/integration -m integration -v

install:
	./netbird-nm install

uninstall:
	./netbird-nm uninstall

clean:
	rm -rf .pytest_cache .ruff_cache build dist src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
