"""Dev-only blob endpoints for memory mode (BE-02).

In `STORAGE_MODE=memory` with `PUBLIC_BASE_URL` set, the in-memory blob fake
mints upload/read URLs pointing here instead of at Azure Blob, so the direct
upload round trip (request URL -> PUT bytes -> confirm) works against a local
server: from the front end, or from `scripts/smoke.py`.

These routes mimic the parts of the Blob REST API a client uses with a SAS URL:
`PUT` stores the body (the `x-ms-blob-type: BlockBlob` header is accepted and
ignored) and answers 201; `GET` returns the bytes with their content type. Each
URL carries `sp` (permissions) and `sig` (HMAC); a bad or missing signature is
403, like an invalid SAS.

They are mounted only in memory mode and are left out of the OpenAPI contract.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from jolt.api.deps import RuntimeDep

router = APIRouter(prefix="/_dev/blob", include_in_schema=False)


def _check(rt, container: str, blob_path: str, request: Request, needed: str) -> None:
    blob = rt.blob
    if container != blob.container:
        raise HTTPException(status_code=404, detail="container not found")
    perm = request.query_params.get("sp", "")
    sig = request.query_params.get("sig", "")
    if not blob.verify(blob_path, perm, sig, needed):
        raise HTTPException(status_code=403, detail="signature does not grant this operation")


@router.put("/{container}/{blob_path:path}", status_code=201)
async def put_blob(container: str, blob_path: str, request: Request, rt: RuntimeDep) -> Response:
    _check(rt, container, blob_path, request, needed="cw")
    data = await request.body()
    rt.blob.put(blob_path, data, request.headers.get("content-type"))
    return Response(status_code=201)


@router.get("/{container}/{blob_path:path}")
async def get_blob(container: str, blob_path: str, request: Request, rt: RuntimeDep) -> Response:
    _check(rt, container, blob_path, request, needed="r")
    stored = rt.blob.get(blob_path)
    if stored is None:
        raise HTTPException(status_code=404, detail="blob not found")
    return Response(content=stored.data, media_type=stored.content_type)
