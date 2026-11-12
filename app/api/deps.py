from datetime import datetime, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.security import decode_access_token
from app.services.auth_service import auth_service

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict:
    """Validates the `Authorization: Bearer <jwt>` header and returns the user row from Supabase."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not credentials:
        raise unauthorized
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise unauthorized

    user = auth_service.get_user_by_id(payload["sub"])
    if not user or not user["is_verified"]:
        raise unauthorized

    # Tokens issued before the last password change are no longer valid
    issued_at = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
    if issued_at.replace(microsecond=0) < datetime.fromisoformat(user["password_changed_at"]).replace(microsecond=0):
        raise unauthorized
    return user


def spam_detector_on():
    """Spam scoring, the spam guide and the spam model answer only while the spam detector is part of this app."""
    if not settings.SPAM_DETECTOR_ENABLED:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The spam detector is moving to its own app and is switched off here.")
