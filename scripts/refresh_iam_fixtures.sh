#!/usr/bin/env bash
# Regenerate infra/bootstrap/iam-fixtures/*.json from dev synth + AWS APIs.
#
# Requires: .venv, JSII_RUNTIME_PACKAGE_CACHE_ROOT (set below), AWS credentials
# for cloudformation:DescribeType and HTTPS to servicereference.us-east-1.amazonaws.com.
#
# Usage:
#   AWS_PROFILE=your-profile AWS_REGION=us-east-1 \
#     ACCOUNT=000000000000 ./scripts/refresh_iam_fixtures.sh
#
# Consolidate from legacy per-file dirs (no AWS):
#   ./scripts/refresh_iam_fixtures.sh --from-legacy

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export JSII_RUNTIME_PACKAGE_CACHE_ROOT="${JSII_RUNTIME_PACKAGE_CACHE_ROOT:-$ROOT/.jsii-cache}"

ACCOUNT="${ACCOUNT:-000000000000}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PY="${ROOT}/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "Missing $PY; use the project Python 3.11 venv." >&2
  exit 1
fi

exec "$PY" -m infra.iam_fixture_refresh --account "$ACCOUNT" --region "$REGION" "$@"
