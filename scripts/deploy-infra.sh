#!/usr/bin/env bash
# Deploy (or update) a Jolt backend environment on Azure in one command (BE-02).
#
#   scripts/deploy-infra.sh                    # dev: infra + build image + roll out app
#   scripts/deploy-infra.sh --skip-build       # infra/config change only, keep the running image
#   scripts/deploy-infra.sh --image <ref>      # deploy a specific image (rollback)
#   scripts/deploy-infra.sh --what-if          # preview the changes, deploy nothing
#   scripts/deploy-infra.sh --env prod --promote <git-sha>   # prod: copy a dev image, no build
#
# Options:
#   --env <name>         environment (default: dev) -> infra/main.<name>.bicepparam
#   --location <region>  deployment metadata region (default: the params file's `location`)
#   --image <ref>        image to run; skips the build
#   --promote <tag>      copy jolt-api:<tag> from the promotion source registry
#                        (promoteFromEnvironment, i.e. dev) into this one and run it
#   --skip-build         reuse the image the app runs now (the app must exist)
#   --local-build        build with local Docker and push, instead of `az acr build`
#   --smoke              run scripts/smoke.py against the app afterwards
#                        (prod: the public checks, dev sign-in being off)
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
PROMOTE=""

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
    --promote) PROMOTE="$2"; shift 2 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

PARAMS="infra/main.${ENV_NAME}.bicepparam"
[[ -f "$PARAMS" ]] || die "no parameters file $PARAMS"

# Read a plain string `param <name> = '<value>'` from the params file.
param_value() {
  sed -n "s/^param $1 = '\\([^']*\\)'.*/\\1/p" "$PARAMS" | head -n1
}
# Read an unquoted value (bool / int): `param <name> = <value>`.
param_raw() {
  sed -n "s/^param $1 = \\([A-Za-z0-9_-]*\\).*/\\1/p" "$PARAMS" | head -n1
}

RG="$(param_value resourceGroupName)"
[[ -n "$RG" ]] || die "resourceGroupName not found in $PARAMS"
[[ -n "$LOCATION" ]] || LOCATION="$(param_value location)"
[[ -n "$LOCATION" ]] || die "pass --location (no location in $PARAMS)"
APP_NAME=jolt-api
DEV_AUTH_ENABLED="$(param_raw devAuthEnabled)"
CUSTOM_DOMAIN="$(sed -n "s/^var customDomain = '\\([^']*\\)'.*/\\1/p" "$PARAMS" | head -n1)"
[[ -n "$CUSTOM_DOMAIN" ]] || CUSTOM_DOMAIN="$(param_value customDomainName)"
PROMOTE_FROM="$(param_value promoteFromEnvironment)"
# GitHub variable names: dev uses the plain names (deploy-dev.yml), other
# environments a prefix (deploy-prod.yml reads PROD_*).
GH_PREFIX=""
[[ "$ENV_NAME" == "dev" ]] || GH_PREFIX="$(echo "$ENV_NAME" | tr '[:lower:]' '[:upper:]')_"

if [[ "$ENV_NAME" == "prod" && -z "$IMAGE" && -z "$PROMOTE" && "$SKIP_BUILD" == 0 && "$WHAT_IF" == 0 ]]; then
  die "prod runs images promoted from dev, never local builds: pass --promote <git-sha> (or --image / --skip-build)"
fi

command -v az >/dev/null || die "Azure CLI (az) not found: https://learn.microsoft.com/cli/azure/install-azure-cli"
az account show >/dev/null 2>&1 || die "not logged in: run 'az login' and 'az account set -s <subscription>'"
SUBSCRIPTION="$(az account show --query id -o tsv)"
log "subscription $SUBSCRIPTION, resource group $RG, region $LOCATION"

az bicep version >/dev/null 2>&1 || az bicep install

RG_EXISTS="$(az group exists -n "$RG")"

# ---- Secrets: generate once, only if Key Vault does not hold them yet --------
# token-hash-pepper (TOKEN_HASH_PEPPER) and dev-auth-secret (DEV_AUTH_SECRET,
# BE-03). Checked through ARM (secret metadata, no value), so the deployer needs
# no Key Vault data-plane role.
export JOLT_TOKEN_HASH_PEPPER=""
export JOLT_DEV_AUTH_SECRET_INIT=""
KV_ID=""
if [[ "$RG_EXISTS" == "true" ]]; then
  KV_ID="$(az keyvault list -g "$RG" --query "[?starts_with(name, 'kv-jolt-${ENV_NAME}-')].id | [0]" -o tsv)"
fi
kv_has_secret() { [[ -n "$KV_ID" ]] && az resource show --ids "${KV_ID}/secrets/$1" >/dev/null 2>&1; }
if ! kv_has_secret token-hash-pepper; then
  log "Key Vault has no token-hash-pepper yet: generating one (it will live only in Key Vault)"
  JOLT_TOKEN_HASH_PEPPER="$(openssl rand -base64 48 | tr -d '\n')"
fi
if [[ "$DEV_AUTH_ENABLED" == "true" ]] && ! kv_has_secret dev-auth-secret; then
  log "Key Vault has no dev-auth-secret yet: generating one (dev sign-in for the smoke test)"
  JOLT_DEV_AUTH_SECRET_INIT="$(openssl rand -hex 32)"
fi
# Kept for --smoke on this run; the deployment writes it to Key Vault only once.
GENERATED_DEV_AUTH_SECRET="$JOLT_DEV_AUTH_SECRET_INIT"

# ---- Custom domain (BE-10) ---------------------------------------------------
# The managed certificate is issued by the first deploy that sets
# customDomainName; once it exists, bind it (customDomainCertificateReady).
export JOLT_CUSTOM_DOMAIN_CERT_READY=false
export JOLT_SKIP_CUSTOM_DOMAIN=false
# Azure validates the DNS records when the hostname is added to the app, so
# leave the domain out until it resolves (its CNAME points at the app).
if [[ -n "$CUSTOM_DOMAIN" ]] && ! python3 -c 'import socket, sys; socket.getaddrinfo(sys.argv[1], 443)' "$CUSTOM_DOMAIN" 2>/dev/null; then
  JOLT_SKIP_CUSTOM_DOMAIN=true
  log "custom domain ${CUSTOM_DOMAIN} does not resolve yet: deploying without it (DNS records printed at the end)"
fi
cert_ready() {
  [[ -n "$CUSTOM_DOMAIN" && "$RG_EXISTS" == "true" ]] || return 1
  local n
  n="$(az containerapp env certificate list -g "$RG" -n "cae-jolt-${ENV_NAME}" \
    --managed-certificates-only \
    --query "length([?properties.subjectName=='${CUSTOM_DOMAIN}' && properties.provisioningState=='Succeeded'])" \
    -o tsv 2>/dev/null || echo 0)"
  [[ "${n:-0}" -gt 0 ]]
}
if cert_ready; then
  JOLT_CUSTOM_DOMAIN_CERT_READY=true
  log "managed certificate for ${CUSTOM_DOMAIN} exists: binding it"
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
if [[ -n "$PROMOTE" ]]; then
  # Copy the image dev built and smoke-tested; prod never rebuilds it.
  [[ -n "$PROMOTE_FROM" ]] || die "--promote: no promoteFromEnvironment in $PARAMS"
  SRC_RG="jolt-${PROMOTE_FROM}"
  SRC_ACR="$(az acr list -g "$SRC_RG" --query '[0].name' -o tsv)"
  [[ -n "$SRC_ACR" ]] || die "--promote: no registry in $SRC_RG"
  az acr repository show -n "$SRC_ACR" --image "jolt-api:${PROMOTE}" -o none \
    || die "--promote: jolt-api:${PROMOTE} is not in $SRC_ACR (deploy it to ${PROMOTE_FROM} first)"
  ACR=""
  if [[ "$RG_EXISTS" == "true" ]]; then
    ACR="$(az acr list -g "$RG" --query '[0].name' -o tsv)"
  fi
  if [[ -z "$ACR" ]]; then
    log "phase 1/2: infrastructure without the app (no registry to copy into yet)"
    OUTPUTS="$(deploy '')"
    ACR="$(output registryName <<<"$OUTPUTS")"
    JOLT_TOKEN_HASH_PEPPER=""
    JOLT_DEV_AUTH_SECRET_INIT=""
    RG_EXISTS=true
  fi
  log "copying ${SRC_ACR}/jolt-api:${PROMOTE} to ${ACR} (az acr import, same digest)"
  az acr import -n "$ACR" --source "${SRC_ACR}.azurecr.io/jolt-api:${PROMOTE}" \
    --image "jolt-api:${PROMOTE}" --force
  IMAGE="$(az acr show -n "$ACR" --query loginServer -o tsv)/jolt-api:${PROMOTE}"
fi

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
    # The secrets now exist; do not write them again.
    JOLT_TOKEN_HASH_PEPPER=""
    JOLT_DEV_AUTH_SECRET_INIT=""
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
KEYVAULT_NAME="$(output keyVaultName <<<"$OUTPUTS")"

# First deploy with a custom domain: the certificate was just requested. If it
# is issued already (DNS in place), bind it now; else say what to do.
if [[ -n "$CUSTOM_DOMAIN" && "$JOLT_CUSTOM_DOMAIN_CERT_READY" != "true" ]]; then
  RG_EXISTS=true
  if [[ "$JOLT_SKIP_CUSTOM_DOMAIN" != "true" ]] && cert_ready; then
    log "managed certificate for ${CUSTOM_DOMAIN} issued: binding it"
    JOLT_CUSTOM_DOMAIN_CERT_READY=true
    OUTPUTS="$(deploy "$IMAGE")"
  else
    log "custom domain ${CUSTOM_DOMAIN}: not serving yet. DNS records needed (at the domain's DNS host):"
    log "  CNAME ${CUSTOM_DOMAIN} -> ${FQDN}"
    log "  TXT   asuid.${CUSTOM_DOMAIN} -> $(output customDomainVerificationId <<<"$OUTPUTS")"
    log "then rerun: scripts/deploy-infra.sh --env ${ENV_NAME} --skip-build"
  fi
fi
PUBLIC_URL="$(output publicUrl <<<"$OUTPUTS")"

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
  Public URL:     ${PUBLIC_URL}
  MCP (SSE):      $(output mcpServerUrl <<<"$OUTPUTS")
  Image:          ${IMAGE}
  Resource group: ${RG}

GitHub Actions repository variables for .github/workflows/deploy-${ENV_NAME}.yml:
  AZURE_TENANT_ID=$(output tenantId <<<"$OUTPUTS")
  AZURE_SUBSCRIPTION_ID=$(output subscriptionId <<<"$OUTPUTS")
  ${GH_PREFIX}AZURE_CLIENT_ID=${GH_CLIENT_ID}
  ${GH_PREFIX}AZURE_RESOURCE_GROUP=${RG}
  ${GH_PREFIX}ACR_NAME=$(output registryName <<<"$OUTPUTS")
  ${GH_PREFIX}CONTAINER_APP_NAME=$(output containerAppName <<<"$OUTPUTS")
  ${GH_PREFIX}KEYVAULT_NAME=${KEYVAULT_NAME}
  ${GH_PREFIX}PUBLIC_URL=${PUBLIC_URL}
EOF

if [[ "$SET_GH_VARS" == 1 ]]; then
  command -v gh >/dev/null || die "--set-github-vars needs the GitHub CLI (gh)"
  [[ -n "$GH_CLIENT_ID" ]] || die "no GitHub deploy identity (githubRepository is empty in $PARAMS)"
  gh variable set AZURE_TENANT_ID -b "$(output tenantId <<<"$OUTPUTS")"
  gh variable set AZURE_SUBSCRIPTION_ID -b "$(output subscriptionId <<<"$OUTPUTS")"
  gh variable set "${GH_PREFIX}AZURE_CLIENT_ID" -b "$GH_CLIENT_ID"
  gh variable set "${GH_PREFIX}AZURE_RESOURCE_GROUP" -b "$RG"
  gh variable set "${GH_PREFIX}ACR_NAME" -b "$(output registryName <<<"$OUTPUTS")"
  gh variable set "${GH_PREFIX}CONTAINER_APP_NAME" -b "$(output containerAppName <<<"$OUTPUTS")"
  gh variable set "${GH_PREFIX}KEYVAULT_NAME" -b "$KEYVAULT_NAME"
  gh variable set "${GH_PREFIX}PUBLIC_URL" -b "$PUBLIC_URL"
  log "GitHub variables set"
fi

if [[ "$RUN_SMOKE" == 1 && "$DEV_AUTH_ENABLED" != "true" ]]; then
  # No dev sign-in (prod): only the checks that need no account.
  log "smoke test (public checks)"
  ORIGIN="$(sed -n "/^param appOrigins = \[/,/\]/s/.*'\(https:[^']*\)'.*/\1/p" "$PARAMS" | head -n1)"
  python3 scripts/smoke.py --public --expect-dev-auth-off --base-url "$PUBLIC_URL" \
    ${ORIGIN:+--origin "$ORIGIN"}
  exit $?
fi

if [[ "$RUN_SMOKE" == 1 ]]; then
  # The smoke test signs in with POST /v1/auth/dev, which needs the dev-auth
  # secret: the one generated on this run, else $JOLT_DEV_AUTH_SECRET, else read
  # from Key Vault (needs data access, e.g. via developerPrincipalId).
  export JOLT_DEV_AUTH_SECRET="${GENERATED_DEV_AUTH_SECRET:-${JOLT_DEV_AUTH_SECRET:-}}"
  if [[ -z "$JOLT_DEV_AUTH_SECRET" ]]; then
    JOLT_DEV_AUTH_SECRET="$(az keyvault secret show --vault-name "$KEYVAULT_NAME" \
      -n dev-auth-secret --query value -o tsv 2>/dev/null || true)"
  fi
  [[ -n "$JOLT_DEV_AUTH_SECRET" ]] || log "warning: no dev-auth secret; set JOLT_DEV_AUTH_SECRET for the smoke test"
  log "smoke test"
  python3 scripts/smoke.py --base-url "$BASE_URL"
fi
