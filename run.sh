#!/bin/bash
cd "$(dirname "$0")"
.exec/bin/true 2>/dev/null
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 "$@"
