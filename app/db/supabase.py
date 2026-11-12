import threading

from fastapi import HTTPException, status
from supabase import Client, create_client

from app.core.config import settings

# One client per thread. The Supabase client keeps a single HTTP/2 connection that isn't safe to
# share between threads, and requests, background sends and the scheduler all run on different
# threads, so sharing one client caused intermittent "server disconnected" errors.
_local = threading.local()


def is_supabase_configured() -> bool:
    return bool(settings.SUPABASE_URL.strip() and settings.SUPABASE_SERVICE_ROLE_KEY.strip())


def get_supabase() -> Client:
    if not is_supabase_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in backend/.env.",
        )
    client = getattr(_local, "client", None)
    if client is None:
        client = _local.client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    return client
