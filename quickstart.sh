#!/usr/bin/env bash
# Native Docker is the default. Portable WASM evidence is explicitly identified.
set -Eeuo pipefail
set +x
umask 077
HVM_QUICKSTART_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
HVM_QUICKSTART_MODE=local
HVM_QUICKSTART_OUTPUT=
HVM_QUICKSTART_TEMP=
HVM_QUICKSTART_PROJECT=
HVM_QUICKSTART_CONTAINER=

usage() {
  cat <<'USAGE'
Usage: ./quickstart.sh [--local-proof | --portable-proof] [--output DIRECTORY]

Default --local-proof:
  Requires Docker Engine and Compose v2. Builds the locked Python proof runner;
  starts disposable PostgreSQL 17/pgvector and Redis with no published ports;
  applies every migration; seeds isolated tenants; executes the authenticated
  multi-agent proof, receipt verification, bounded model, and concurrency tests.
  Copies reports to DIRECTORY and removes only this run's containers/volumes.

--portable-proof:
  Requires Python 3.12+, Node 20+ and npm. Uses an explicit PostgreSQL/WASM proof
  with one shared session. It does not prove native concurrent-session behavior.
  Set HIVEMIND_TEST_PYTHON to an existing prepared virtualenv interpreter, or this
  script creates .quickstart-venv and installs the hash-locked development set.

Neither mode provisions a public endpoint, purchases infrastructure, uses live
model/Stripe credentials, or claims that native host apps executed these tests.
Production VPS provisioning is provided separately by deploy/bootstrap_infra.sh.
USAGE
}

fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"; }
cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$HVM_QUICKSTART_PROJECT" && -n "$HVM_QUICKSTART_TEMP" ]]; then
    docker compose --env-file "$HVM_QUICKSTART_TEMP/proof.env" \
      -f "$HVM_QUICKSTART_ROOT/deploy/docker-compose.proof.yml" \
      -p "$HVM_QUICKSTART_PROJECT" down --volumes --remove-orphans >/dev/null 2>&1 || \
      printf 'Cleanup incomplete; remove Compose project %s after inspection.\n' "$HVM_QUICKSTART_PROJECT" >&2
  fi
  if [[ -n "$HVM_QUICKSTART_TEMP" ]]; then rm -rf -- "$HVM_QUICKSTART_TEMP"; fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

while (($#)); do
  case "$1" in
    --local-proof) HVM_QUICKSTART_MODE=local; shift ;;
    --portable-proof) HVM_QUICKSTART_MODE=portable; shift ;;
    --output) (($# >= 2)) || fail '--output requires a directory'; HVM_QUICKSTART_OUTPUT=$2; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; fail "Unknown option: $1" ;;
  esac
done
cd -- "$HVM_QUICKSTART_ROOT"
need date
need mktemp
need tee
HVM_QUICKSTART_TEMP=$(mktemp -d "${TMPDIR:-/tmp}/hivemind-proof.XXXXXXXX")
if [[ -z "$HVM_QUICKSTART_OUTPUT" ]]; then
  HVM_QUICKSTART_OUTPUT="$HVM_QUICKSTART_ROOT/evidence/quickstart-$(date -u +%Y%m%dT%H%M%SZ)-${HVM_QUICKSTART_TEMP##*.}"
fi
[[ ! -e "$HVM_QUICKSTART_OUTPUT" && ! -L "$HVM_QUICKSTART_OUTPUT" ]] || fail 'Output already exists; select a new proof directory'
mkdir -p -- "$HVM_QUICKSTART_OUTPUT"
HVM_QUICKSTART_OUTPUT=$(cd -- "$HVM_QUICKSTART_OUTPUT" && pwd -P)

if [[ "$HVM_QUICKSTART_MODE" == portable ]]; then
  need node
  need npm
  node -e 'if(Number(process.versions.node.split(".")[0]) < 20) process.exit(1)' || fail 'Node 20 or newer is required'
  if [[ -z "${HIVEMIND_TEST_PYTHON:-}" ]]; then
    need python3
    python3 -c 'import sys; assert sys.version_info >= (3,12)' || fail 'Python 3.12 or newer is required'
    if [[ ! -x .quickstart-venv/bin/python ]]; then python3 -m venv .quickstart-venv; fi
    HIVEMIND_TEST_PYTHON="$HVM_QUICKSTART_ROOT/.quickstart-venv/bin/python"
    "$HIVEMIND_TEST_PYTHON" -m pip install --require-hashes -r requirements-dev.lock
  fi
  [[ -x "$HIVEMIND_TEST_PYTHON" ]] || fail 'HIVEMIND_TEST_PYTHON must be an executable interpreter path'
  "$HIVEMIND_TEST_PYTHON" -c 'import fastmcp, psycopg, asgi_lifespan; import sys; assert sys.version_info >= (3,12)' || fail 'The selected Python needs the locked development dependencies'
  export HIVEMIND_TEST_PYTHON
  export PYTHONPATH="$HVM_QUICKSTART_ROOT/src"
  npm ci --prefix tests --ignore-scripts --no-audit --no-fund
  node tests/pglite-investor.mjs --output "$HVM_QUICKSTART_OUTPUT/investor-proof.json" \
    2>&1 | tee "$HVM_QUICKSTART_OUTPUT/execution.log"
  "$HIVEMIND_TEST_PYTHON" tests/investor_proof_harness.py --verify "$HVM_QUICKSTART_OUTPUT/investor-proof.json" \
    2>&1 | tee -a "$HVM_QUICKSTART_OUTPUT/execution.log"
  "$HIVEMIND_TEST_PYTHON" tests/verify_ledger_model.py --output "$HVM_QUICKSTART_OUTPUT/ledger-model-proof.json" \
    2>&1 | tee -a "$HVM_QUICKSTART_OUTPUT/execution.log"
  printf 'PASS portable proof and audit verification; native concurrency was not tested.\n'
  printf 'Proof reports: %s\n' "$HVM_QUICKSTART_OUTPUT"
  exit 0
fi

need docker
need od
need tr
docker info >/dev/null 2>&1 || fail 'Docker Engine is not reachable'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required'
HVM_QUICKSTART_PROJECT="hvmproof-$(date -u +%Y%m%d%H%M%S)-$(od -An -N5 -tx1 /dev/urandom | tr -d ' \n')"
HVM_QUICKSTART_CONTAINER="${HVM_QUICKSTART_PROJECT}-runner"
printf 'HVM_PROOF_ADMIN_PASSWORD=%s\nHVM_PROOF_APP_PASSWORD=%s\n' \
  "$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')" \
  "$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')" > "$HVM_QUICKSTART_TEMP/proof.env"
compose() {
  docker compose --env-file "$HVM_QUICKSTART_TEMP/proof.env" \
    -f "$HVM_QUICKSTART_ROOT/deploy/docker-compose.proof.yml" -p "$HVM_QUICKSTART_PROJECT" "$@"
}
compose config --quiet
compose build proof
compose up --detach --wait --wait-timeout 120 postgres redis
HVM_QUICKSTART_STATUS=0
compose run --no-deps --name "$HVM_QUICKSTART_CONTAINER" proof \
  2>&1 | tee "$HVM_QUICKSTART_OUTPUT/execution.log" || HVM_QUICKSTART_STATUS=$?
docker cp "$HVM_QUICKSTART_CONTAINER:/evidence/." "$HVM_QUICKSTART_OUTPUT/" || fail 'Could not copy proof artifacts'
docker inspect --format \
  '{"container":{{json .Name}},"image_id":{{json .Image}},"configured_image":{{json .Config.Image}}}' \
  "$HVM_QUICKSTART_CONTAINER" "$(compose ps --quiet postgres)" "$(compose ps --quiet redis)" \
  > "$HVM_QUICKSTART_OUTPUT/container-image-identities.jsonl"
if ((HVM_QUICKSTART_STATUS != 0)); then
  printf 'FAIL investor proof; available evidence: %s\n' "$HVM_QUICKSTART_OUTPUT" >&2
  exit "$HVM_QUICKSTART_STATUS"
fi
printf 'PASS native PostgreSQL proof, audit verification, bounded model, and concurrency suite.\n'
printf 'Proof reports: %s\n' "$HVM_QUICKSTART_OUTPUT"
