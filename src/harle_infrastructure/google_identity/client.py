from asyncio import to_thread
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode

import httpx
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from harle_domain.accounts import GoogleIdentity
from harle_utils import InvalidOAuthError, OAuthProviderError

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True, slots=True)
class GoogleOAuthClient:
    client_id: str
    client_secret: str
    redirect_uri: str

    def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            },
        )
        return f"{AUTHORIZATION_ENDPOINT}?{query}"

    async def exchange(
        self,
        *,
        code: str,
        code_verifier: str,
        expected_nonce: str,
    ) -> GoogleIdentity:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    TOKEN_ENDPOINT,
                    data={
                        "code": code,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "redirect_uri": self.redirect_uri,
                        "grant_type": "authorization_code",
                        "code_verifier": code_verifier,
                    },
                )
        except httpx.HTTPError as exc:
            raise OAuthProviderError from exc
        if response.status_code != 200:
            raise InvalidOAuthError
        try:
            payload = response.json()
        except ValueError as exc:
            raise OAuthProviderError from exc
        if not isinstance(payload, Mapping):
            raise InvalidOAuthError
        raw_token = payload.get("id_token")
        if not isinstance(raw_token, str) or not raw_token:
            raise InvalidOAuthError
        try:
            claims = await to_thread(
                id_token.verify_oauth2_token,
                raw_token,
                GoogleRequest(),
                self.client_id,
            )
        except (GoogleAuthError, ValueError) as exc:
            raise InvalidOAuthError from exc
        if not isinstance(claims, Mapping):
            raise InvalidOAuthError
        typed_claims = cast(Mapping[str, object], claims)
        if typed_claims.get("nonce") != expected_nonce:
            raise InvalidOAuthError
        subject = typed_claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise InvalidOAuthError
        display_name = typed_claims.get("name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = "Harle user"
        email = typed_claims.get("email")
        if not isinstance(email, str) or not email.strip():
            raise InvalidOAuthError
        email_verified = typed_claims.get("email_verified")
        verified = email_verified is True or email_verified == "true"
        if not verified:
            raise InvalidOAuthError
        return GoogleIdentity(
            subject=subject,
            display_name=display_name.strip(),
            email=email.strip().lower(),
            email_verified=True,
        )
