# NVHive — development & distribution commands

.PHONY: install dev test lint build publish clean

# Install for users
install:
	pip install .

# Install for development
dev:
	pip install -e ".[dev]"

# Run tests
test:
	python -m pytest tests/ -v

# Run tests with coverage
coverage:
	python -m pytest tests/ --cov=nvh --cov-report=term-missing

# Lint
lint:
	python -m ruff check nvh/ tests/

# Type check
typecheck:
	python -m mypy nvh/ --ignore-missing-imports --no-strict

# Build package (wheel + sdist)
build:
	python -m build

# Publish to TestPyPI
publish-test:
	python -m twine upload --repository testpypi dist/*

# Publish to PyPI
publish:
	python -m twine upload dist/*

# Build web UI
web:
	cd web && npm ci && npm run build

# Start API server
serve:
	nvh serve

# Run all checks (what CI does)
ci: lint typecheck test

# Clean build artifacts
clean:
	rm -rf dist/ build/ *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
