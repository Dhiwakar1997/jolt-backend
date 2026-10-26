#!/usr/bin/env bash
# Deploy (or update) a Jolt backend environment on Azure in one command (BE-02).
#
#   scripts/deploy-infra.sh                    # dev: infra + build image + roll out app
#   scripts/deploy-infra.sh --skip-build       # infra/config change only, keep the running image
#   scripts/deploy-infra.sh --image <ref>      # deploy a specific image (rollback)
#   scripts/deploy-infra.sh --what-if          # preview the changes, deploy nothing
#
# Options:
#   --env <name>         environment (default: dev) -> infra/main.<name>.bicepparam
#   --location <region>  deployment metadata region (default: the params file's `location`)
#   --image <ref>        image to run; skips the build
#   --skip-build         reuse the image the app runs now (the app must exist)
#   --local-build        build with local Docker and push, instead of `az acr build`
#   --smoke              run scripts/smoke.py against the app afterwards
#   --set-github-vars    write the GitHub Actions variables with `gh variable set`
#   --what-if            run `az deployment sub what-if` and stop
#
# Needs: Azure CLI >= 2.61 logged in (`az login`, `az account set -s <sub>`) as a
# principal that can create resource groups and role assignments (Owner, or
# Contributor + User Access Administrator) on the subscription; git; Docker only
# with --local-build; Python with requirements.txt only with --smoke.
# See docs/DEPLOY.md for the full walkthrough.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ENV_NAME=dev
LOCATION=""
IMAGE=""
SKIP_BUILD=0
LOCAL_BUILD=0
RUN_SMOKE=0
SET_GH_VARS=0
WHAT_IF=0

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_NAME="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --image) IMAGE="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --local-build) LOCAL_BUILD=1; shift ;;
    --smoke) RUN_SMOKE=1; shift ;;
    --set-github-vars) SET_GH_VARS=1; shift ;;
    --what-if) WHAT_IF=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

PARAMS="infra/main.${ENV_NAME}.bicepparam"
[[ -f "$PARAMS" ]] || die "no parameters file $PARAMS"

# Read a plain string `param <name> = '<value>'` from the params file.
param_value() {
  sed -n "s/^param $1 = '\\([^']*\\)'.*/\\1/p" "$PARAMS" | head -n1
}

RG="$(param_value resourceGroupName)"
[[ -n "$RG" ]] || die "resourceGroupName not found in $PARAMS"
[[ -n "$LOCATION" ]] || LOCATION="$(param_value location)"
[[ -n "$LOCATION" ]] || die "pass --location (no location in $PARAMS)"
APP_NAME=jolt-api

command -v az >/dev/null || die "Azure CLI (az) not found: https://learn.microsoft.com/cli/azure/install-azure-cli"
az account show >/dev/null 2>&1 || die "not logged in: run 'az login' and 'az account set -s <subscription>'"
SUBSCRIPTION="$(az account show --query id -o tsv)"
log "subscription $SUBSCRIPTION, resource group $RG, region $LOCATION"

az bicep version >/dev/null 2>&1 || az bicep install

RG_EXISTS="$(az group exists -n "$RG")"

# ---- Token-hash pepper: generate once, only if Key Vault does not hold one ----
# Checked through ARM (secret metadata, no value), so the deployer needs no
# Key Vault data-plane role.
export JOLT_TOKEN_HASH_PEPPER=""
NEED_PEPPER=1
if [[ "$RG_EXISTS" == "true" ]]; then
  KV_ID="$(az keyvault list -g "$RG" --query "[?starts_with(name, 'kv-jolt-${ENV_NAME}-')].id | [0]" -o tsv)"
  if [[ -n "$KV_ID" ]] && az resource show --ids "${KV_ID}/secrets/token-hash-pepper" >/dev/null 2>&1; then
    NEED_PEPPER=0
  fi
fi
if [[ "$NEED_PEPPER" == 1 ]]; then
  log "Key Vault has no token-hash-pepper yet: generating one (it will live only in Key Vault)"
  JOLT_TOKEN_HASH_PEPPER="$(openssl rand -base64 48 | tr -d '\n')"
fi

deploy() {
  # $1 = image ('' = infra only). Prints the deployment outputs as JSON.
  local image="$1" name
  name="jolt-${ENV_NAME}-$(date -u +%Y%m%d%H%M%S)"
  JOLT_IMAGE="$image" az deployment sub create \
    --name "$name" \
    --location "$LOCATION" \
    --template-file infra/main.bicep \
    --parameters "$PARAMS" \
    --query properties.outputs -o json
}

output() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]]["value"])' "$1"; }

if [[ "$WHAT_IF" == 1 ]]; then
  JOLT_IMAGE="${IMAGE:-}" az deployment sub what-if \
    --location "$LOCATION" --template-file infra/main.bicep --parameters "$PARAMS"
  exit 0
fi

# ---- Decide which image to run ----------------------------------------------
if [[ -z "$IMAGE" && "$SKIP_BUILD" == 1 ]]; then
  [[ "$RG_EXISTS" == "true" ]] || die "--skip-build: nothing deployed yet"
  IMAGE="$(az containerapp show -g "$RG" -n "$APP_NAME" \
    --query 'properties.template.containers[0].image' -o tsv 2>/dev/null || true)"
  [[ -n "$IMAGE" ]] || die "--skip-build: $APP_NAME does not exist yet; run without it once"
  log "keeping the running image $IMAGE"
fi

if [[ -z "$IMAGE" ]]; then
  # Build from the current commit. The registry must exist first: on a fresh
  # environment, deploy everything except the app, build, then deploy the app.
  ACR=""
  if [[ "$RG_EXISTS" == "true" ]]; then
    ACR="$(az acr list -g "$RG" --query '[0].name' -o tsv)"
  fi
  if [[ -z "$ACR" ]]; then
    log "phase 1/2: infrastructure without the app (no registry to build into yet)"
    OUTPUTS="$(deploy '')"
    ACR="$(output registryName <<<"$OUTPUTS")"
    # The pepper secret now exists; do not write it again.
    JOLT_TOKEN_HASH_PEPPER=""
  fi
  LOGIN_SERVER="$(az acr show -n "$ACR" --query loginServer -o tsv)"
  TAG="$(git rev-parse HEAD)"
  if [[ -n "$(git status --porcelain)" ]]; then
    TAG="${TAG}-dirty-$(date -u +%Y%m%d%H%M%S)"
    log "working tree has uncommitted changes: tagging the image $TAG"
  fi
  IMAGE="${LOGIN_SERVER}/jolt-api:${TAG}"
  if [[ "$LOCAL_BUILD" == 1 ]]; then
    log "building $IMAGE with local Docker"
    docker build --build-arg "APP_VERSION=${TAG}" -t "$IMAGE" .
    az acr login -n "$ACR"
    docker push "$IMAGE"
  else
    log "building $IMAGE with ACR Tasks (use --local-build if Tasks are not allowed on this subscription)"
    az acr build -r "$ACR" -t "jolt-api:${TAG}" --build-arg "APP_VERSION=${TAG}" .
  fi
fi

# ---- Deploy everything, app included ----------------------------------------
log "deploying ${RG} with image ${IMAGE}"
OUTPUTS="$(deploy "$IMAGE")"
FQDN="$(output containerAppFqdn <<<"$OUTPUTS")"
BASE_URL="https://${FQDN}"

log "waiting for ${BASE_URL}/health"
for _ in $(seq 1 40); do
  if curl -fsS --max-time 10 "${BASE_URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 6
done
curl -fsS --max-time 10 "${BASE_URL}/health" || die "health check failed: see docs/DEPLOY.md, Troubleshooting"
echo

GH_CLIENT_ID="$(output githubDeployClientId <<<"$OUTPUTS")"
cat >&2 <<EOF

Deployed.
  API:            ${BASE_URL}
  MCP (SSE):      ${BASE_URL}/mcp/sse
  Image:          ${IMAGE}
  Resource group: ${RG}

GitHub Actions repository variables for .github/workflows/deploy-dev.yml:
  AZURE_CLIENT_ID=${GH_CLIENT_ID}
  AZURE_TENANT_ID=$(output tenantId <<<"$OUTPUTS")
  AZURE_SUBSCRIPTION_ID=$(output subscriptionId <<<"$OUTPUTS")
  AZURE_RESOURCE_GROUP=${RG}
  ACR_NAME=$(output registryName <<<"$OUTPUTS")
  CONTAINER_APP_NAME=$(output containerAppName <<<"$OUTPUTS")
EOF

if [[ "$SET_GH_VARS" == 1 ]]; then
  command -v gh >/dev/null || die "--set-github-vars needs the GitHub CLI (gh)"
  [[ -n "$GH_CLIENT_ID" ]] || die "no GitHub deploy identity (githubRepository is empty in $PARAMS)"
  gh variable set AZURE_CLIENT_ID -b "$GH_CLIENT_ID"
  gh variable set AZURE_TENANT_ID -b "$(output tenantId <<<"$OUTPUTS")"
  gh variable set AZURE_SUBSCRIPTION_ID -b "$(output subscriptionId <<<"$OUTPUTS")"
  gh variable set AZURE_RESOURCE_GROUP -b "$RG"
  gh variable set ACR_NAME -b "$(output registryName <<<"$OUTPUTS")"
  gh variable set CONTAINER_APP_NAME -b "$(output containerAppName <<<"$OUTPUTS")"
  log "GitHub variables set"
fi

if [[ "$RUN_SMOKE" == 1 ]]; then
  log "smoke test"
  python3 scripts/smoke.py --base-url "$BASE_URL"
fi
