# 0001. Images are published to GHCR from CI, tagged by commit

- Status: Proposed
- Date: 2026-10-27
- Scope: the image registry only. Where and how Azure runs the image is a
  separate decision.

## Context

The image is built by `deploy-dev.yml` on pushes to `main` and pushed to Azure
Container Registry (ACR), which the Container App pulls from with its Managed
Identity ([DEPLOY.md](../DEPLOY.md), "Registry: ACR, not GHCR"). Pull requests
never build the image, so a broken Dockerfile is first seen on `main`, and an
image exists only once Azure is set up.

## Decision

GitHub Container Registry is the registry for the backend image, replacing ACR.

- `.github/workflows/image.yml` builds the image on every pull request and
  pushes it to `ghcr.io/dhiwakar1997/jolt-backend` on pushes to `main` and on
  `v*` tags, only after the CI checks (`ci.yml`: lint, tests, contract,
  dependency audit) pass.
- It pushes with the workflow's own `GITHUB_TOKEN` (`packages: write`); no
  registry credential is stored. The package is private.
- Tags: `sha-<short>` for every pushed build, `main` for the tip of `main`, and
  the version (`1.2.3`) for a `v1.2.3` tag. There is no `latest`: a deploy names
  a commit. `GET /health` reports the full commit SHA (`APP_VERSION`).
- One image, `linux/amd64`, one process (uvicorn), a non-root user, no
  configuration baked in: every setting comes from the environment at run time.
  `.dockerignore` keeps `.git`, `.github`, env files, tests and docs out.

## Consequences

- Every pull request proves the image builds before merge.
- The image has a `HEALTHCHECK` on `GET /health`. It is liveness only: the route
  touches no store, so it stays healthy through a Cosmos or Blob outage. Under
  Managed Identity the app does not contact Cosmos at start-up, so an
  unreachable database does not stop it starting either; requests fail instead.
  What does stop start-up (missing serving secrets, or Cosmos unreachable when
  signing in with a key) exits the process and the host restarts it.
  Container Apps ignores `HEALTHCHECK` and uses its own probes.
- Until the Azure deploy moves over, `deploy-dev.yml` still builds the same
  commit into ACR, so each `main` commit is built twice and the two images have
  different digests. Retiring ACR (deploy workflows, `infra/`, the pull
  credential) is follow-up work and is not part of this decision.
- Pulling a private GHCR package from Azure needs a token with `read:packages`
  stored as a Container App registry secret. That is the long-lived credential
  the ACR choice avoided (LLD §5.1, "no key material"); the follow-up has to
  accept or replace it.
