#!/usr/bin/env bash
# run-oem-web.sh — Launch the Flask web interface for patch package downloading.
# Dependencies (Flask, packaging) are installed automatically on first run.
# No venv activation needed.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 web_oem_download.py
