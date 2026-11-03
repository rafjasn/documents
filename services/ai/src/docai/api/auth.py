"""JWT validation for the AI service.

The API forwards the user's bearer token; the AI service checks it against the same
key set, issuer and audience, and takes the user from `sub`, as the API does. The user
is never read from a request body or from model output.
"""

from collections.abc import Callable
from typing import Any

import jwt
from fastapi import HTTPException, Request, status

from docai.core.settings import AuthSettings

KeyResolver = Callable[[str], Any]


class TokenVerifier:
    def __init__(
        self, settings: AuthSettings, key_for: KeyResolver | None = None
    ) -> None:
        self.issuer, self.audience = settings.auth_issuer, settings.auth_audience
        if key_for is None:
            client = jwt.PyJWKClient(
                settings.auth_jwks_uri, cache_keys=True, lifespan=300
            )
            key_for = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self.key_for = key_for

    def user_id(self, token: str) -> str:
        try:
            claims = jwt.decode(
                token,
                self.key_for(token),
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "sub"]},
            )
        except (jwt.PyJWTError, ValueError) as error:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error
        subject = str(claims.get("sub") or "")
        if not subject:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has no subject")
        return subject


def bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token
