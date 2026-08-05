#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/oci_arm_retry.py --env-file oci-secrets.env
