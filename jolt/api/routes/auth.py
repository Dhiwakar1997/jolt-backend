"""Sign-in and sign-out (BE-03).

`POST /v1/auth/google` verifies a Google ID token server-side and returns a fresh
**app token** for this device plus the user. `POST /v1/auth/sign-out` revokes the
app token that made the request. `POST /v1/auth/dev` signs in by email without
Google; it is mounted only when `DEV_AUTH_ENABLED` is on (never in prod) and, on a
server with real storage, only with the `X-Jolt-Dev-Auth-Secret` header.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from jolt.api.deps import RuntimeDep, UserDep
from jolt.api.errors import UNAUTHORIZED, ErrorResponse
from jolt.auth.google import GoogleKeysUnavailable, GoogleSignInNotConfigured, GoogleTokenError
from jolt.domain.models import User
from jolt.services.accounts import AccountService, SignedIn

router = APIRouter(prefix="/v1/auth", tags=["auth"])
dev_router = APIRouter(prefix="/v1/auth", tags=["auth"])


class GoogleSignInRequest(BaseModel):
    id_token: str = Field(
        min_length=1,
        max_length=8192,
        description="Google ID token (a JWT) from Google sign-in",
    )


class DevSignInRequest(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$", max_length=254)
    display_name: str | None = Field(default=None, max_length=100)


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    picture_url: str | None
    created_at: datetime


class SignInResponse(BaseModel):
    app_token: str = Field(description="Shown once. Send as `Authorization: Bearer` on /v1.")
    user: UserResponse


def user_response(user: User) -> UserResponse:
    email = user.email or ""
    return UserResponse(
        id=user.id,
        email=email,
        display_name=user.display_name or email.split("@")[0],
        picture_url=user.picture_url,
        created_at=user.created_at,
    )


def _signed_in(result: SignedIn) -> SignInResponse:
    return SignInResponse(app_token=result.app_token, user=user_response(result.user))


@router.post(
    "/google",
    response_model=SignInResponse,
    responses={
        401: {"model": ErrorResponse, "description": "The Google ID token was rejected"},
        503: {
            "model": ErrorResponse,
            "description": "Google sign-in is not configured, or Google's keys are unreachable",
        },
    },
)
async def sign_in_google(body: GoogleSignInRequest, rt: RuntimeDep) -> SignInResponse:
    try:
        identity = await rt.google.verify(body.id_token)
    except GoogleSignInNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GoogleKeysUnavailable as exc:
        detail = "could not reach Google to verify the sign-in, try again"
        raise HTTPException(status_code=503, detail=detail) from exc
    except GoogleTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail=f"invalid Google ID token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return _signed_in(await AccountService(rt).sign_in_google(identity))


@router.post("/sign-out", status_code=204, responses=UNAUTHORIZED)
async def sign_out(user: UserDep, rt: RuntimeDep) -> Response:
    await AccountService(rt).sign_out(user.user_id, user.token_id)
    return Response(status_code=204)


@dev_router.post(
    "/dev",
    response_model=SignInResponse,
    responses={403: {"model": ErrorResponse, "description": "Missing or wrong dev auth secret"}},
)
async def sign_in_dev(
    body: DevSignInRequest,
    rt: RuntimeDep,
    x_jolt_dev_auth_secret: Annotated[str | None, Header()] = None,
) -> SignInResponse:
    """Dev only: sign in by email without Google (mounted when DEV_AUTH_ENABLED).

    The same email always gets the same user. When the server has a
    DEV_AUTH_SECRET (required with real storage), send it as
    `X-Jolt-Dev-Auth-Secret`.
    """
    secret = rt.settings.dev_auth_secret
    if secret and not hmac.compare_digest((x_jolt_dev_auth_secret or "").encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="dev sign-in needs the dev auth secret")
    return _signed_in(await AccountService(rt).sign_in_dev(body.email, body.display_name))
