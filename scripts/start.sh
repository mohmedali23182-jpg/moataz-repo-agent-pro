#!/usr/bin/env bash
set -e
: "${PORT:=8000}"
uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers
