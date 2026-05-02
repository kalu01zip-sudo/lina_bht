# apple_auth.py
"""
Apple Sign-In token verifier.
Fetches Apple's public keys and verifies the identity_token JWT.
"""

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
import json

APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
APPLE_ISSUER   = "https://appleid.apple.com"

# Your Apple App's Bundle ID — set in .env
import os
APPLE_BUNDLE_ID = os.environ.get("APPLE_BUNDLE_ID", "com.yourcompany.skinsense")


async def verify_apple_token(identity_token: str) -> dict:
    """
    Verify Apple identity_token and return the decoded payload.
    
    Returns dict with: sub, email, email_verified
    Raises ValueError if token is invalid.
    """
    # Step 1 — Fetch Apple's public keys
    async with httpx.AsyncClient() as client:
        resp = await client.get(APPLE_KEYS_URL)
        if resp.status_code != 200:
            raise ValueError("Failed to fetch Apple public keys.")
        apple_keys = resp.json().get("keys", [])

    # Step 2 — Get the key ID from token header
    unverified_header = jwt.get_unverified_header(identity_token)
    kid = unverified_header.get("kid")
    if not kid:
        raise ValueError("No 'kid' in Apple token header.")

    # Step 3 — Find matching public key
    matching_key = next((k for k in apple_keys if k["kid"] == kid), None)
    if not matching_key:
        raise ValueError("No matching Apple public key found.")

    # Step 4 — Convert JWK to RSA public key
    public_key = RSAAlgorithm.from_jwk(json.dumps(matching_key))

    # Step 5 — Verify and decode the token
    try:
        payload = jwt.decode(
            identity_token,
            public_key,
            algorithms=["RS256"],
            audience=APPLE_BUNDLE_ID,
            issuer=APPLE_ISSUER,
        )
    except jwt.ExpiredSignatureError:
        raise ValueError("Apple token has expired.")
    except jwt.InvalidTokenError as e:
        raise ValueError(f"Apple token invalid: {str(e)}")

    return payload