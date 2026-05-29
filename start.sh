#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo ""
echo "CardTax starting on http://localhost:8000"
echo ""
exec python -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
