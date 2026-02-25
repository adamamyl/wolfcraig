#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${REPO_ROOT}/logs"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_DIR}/run-${TIMESTAMP}.log"

mkdir -p "${LOG_DIR}"

# Tee everything to log and stdout
exec > >(tee -a "${LOG_FILE}") 2>&1

echo "================================================================"
echo "wolfcraig test run — ${TIMESTAMP}"
echo "repo: ${REPO_ROOT}"
echo "log:  ${LOG_FILE}"
echo "================================================================"
echo ""

cd "${REPO_ROOT}"

# Track pass/fail per tool
declare -A results

run_step() {
    local name="$1"
    shift
    echo "--- ${name} ---"
    if "$@"; then
        results["${name}"]="PASS"
        echo ""
    else
        results["${name}"]="FAIL"
        echo ""
    fi
}

run_step "ruff check"   uv run ruff check .
run_step "ruff format"  uv run ruff format --check .
run_step "mypy"         uv run mypy lib/ scripts/ server_setup.py
run_step "bandit"       uv run bandit -c pyproject.toml -r lib/ scripts/ server_setup.py -ll
run_step "pytest"       uv run pytest -v

echo "================================================================"
echo "SUMMARY"
echo "================================================================"
overall="PASS"
for step in "ruff check" "ruff format" "mypy" "bandit" "pytest"; do
    status="${results[${step}]:-NOT RUN}"
    printf "  %-18s %s\n" "${step}" "${status}"
    if [[ "${status}" != "PASS" ]]; then
        overall="FAIL"
    fi
done
echo ""
echo "  Overall: ${overall}"
echo "================================================================"
echo "Log saved: ${LOG_FILE}"
echo ""

if [[ "${overall}" == "FAIL" ]]; then
    exit 1
fi
