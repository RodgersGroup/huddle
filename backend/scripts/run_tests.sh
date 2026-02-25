#!/bin/bash
# Run Huddle test suite
cd "$(dirname "$0")/.."
source venv/bin/activate
python -m pytest tests/ -v "$@"
