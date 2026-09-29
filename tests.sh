#!/bin/bash
set -Eeuo pipefail
umask 077
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_ROOT}"
poetry run python -m pytest -v --cov=gills --cov-report=term-missing
poetry run gills --version
poetry run gills --help
