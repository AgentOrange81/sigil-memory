.PHONY: test lint fmt demo clean

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/ruff check .

fmt:
	.venv/bin/ruff check --fix .
	.venv/bin/ruff format .

demo:
	bash examples/demo.sh

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ /tmp/sigil-demo
