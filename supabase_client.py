import os
import logging
from supabase import create_client
from typing import Optional, List, Dict, Any

_client = None

logger = logging.getLogger("supabase_client")
if not logger.handlers:
    # basic configuration if not already configured by the app
    logging.basicConfig(level=logging.INFO)


def get_supabase_client() -> Optional[Any]:
    """Create and cache a Supabase client. Returns None if required env vars missing.

    This helper intentionally does not raise so callers can fallback to local DB.
    """
    global _client
    if _client:
        return _client
    url = (os.getenv("SUPABASE_URL") or "").strip()
    key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        logger.debug("Supabase URL or Key not set (SUPABASE_URL/SUPABASE_KEY)")
        return None
    # If the default placeholder value is still present, avoid creating the client
    if "your-project-ref" in url:
        logger.info("SUPABASE_URL appears to be a placeholder (%s); skipping Supabase client creation", url)
        return None
    try:
        _client = create_client(url, key)
        return _client
    except Exception as e:
        logger.exception("Failed to create Supabase client: %s", e)
        return None


def _unwrap_response(resp: Any) -> Any:
    """Normalize responses from different supabase-py versions.

    Returns either the data list or a dict with error information.
    """
    try:
        # new style: object with .data and .error
        data = getattr(resp, "data", None)
        error = getattr(resp, "error", None)
        if data is not None or error is not None:
            return {"data": data, "error": error}
    except Exception:
        pass

    # older style: dict-like
    try:
        if isinstance(resp, dict):
            return {"data": resp.get("data"), "error": resp.get("error")}
    except Exception:
        pass

    # fallback: return raw
    return {"data": None, "error": resp}


def insert_transcript(record: Dict) -> Optional[Dict]:
    supabase = get_supabase_client()
    if not supabase:
        logger.debug("Supabase client not available, skipping insert")
        return None
    try:
        print("Record - ", record)
        resp = supabase.table("transcripts").insert(record).execute()
        normalized = _unwrap_response(resp)
        if normalized.get("error"):
            logger.error("Supabase insert error: %s | record=%s", normalized.get("error"), record)
        else:
            logger.info("Inserted transcript to Supabase: %s", record.get("filename") or "<no-filename>")
        return normalized
    except Exception as e:
        logger.exception("Exception when inserting transcript: %s", e)
        return {"data": None, "error": str(e)}


def get_transcripts_for_user(user_id: str) -> List[Dict]:
    supabase = get_supabase_client()
    if not supabase:
        return []
    try:
        resp = supabase.table("transcripts").select("*").eq("user_id", user_id).order("created_at", desc=True).execute()
        normalized = _unwrap_response(resp)
        return normalized.get("data") or []
    except Exception as e:
        logger.exception("Failed to fetch transcripts for user %s: %s", user_id, e)
        return []


def get_all_transcripts() -> List[Dict]:
    supabase = get_supabase_client()
    if not supabase:
        return []
    try:
        resp = supabase.table("transcripts").select("*").order("created_at", desc=True).execute()
        normalized = _unwrap_response(resp)
        return normalized.get("data") or []
    except Exception as e:
        logger.exception("Failed to fetch all transcripts: %s", e)
        return []


def get_user_from_bearer(token: str) -> Optional[Dict]:
    """Try to resolve a Supabase user from a Bearer token.

    Returns a dict with at least 'id' and 'email' when successful, otherwise None.
    This prefers using the Supabase admin endpoint if available, falling back to
    the client.auth.api.get_user_by_token style call depending on supabase-py version.
    """
    supabase = get_supabase_client()
    if not supabase or not token:
        return None

    def _decode_jwt_unverified(jwt_token: str) -> Optional[Dict]:
        """Decode JWT without verification to extract payload fields.

        This is a best-effort fallback and DOES NOT verify the token. Use only
        to extract non-sensitive identifiers (like the user's id) when the
        Supabase admin endpoint is unavailable.
        """
        try:
            parts = jwt_token.split('.')
            if len(parts) < 2:
                return None
            import base64, json

            def _b64decode(s: str) -> bytes:
                s = s.encode('utf-8')
                # pad base64 string
                rem = len(s) % 4
                if rem > 0:
                    s += b'=' * (4 - rem)
                return base64.urlsafe_b64decode(s)

            payload = _b64decode(parts[1])
            return json.loads(payload.decode('utf-8'))
        except Exception:
            return None

    try:
        # Newer supabase-py exposes auth.get_user(jwt)
        if hasattr(supabase.auth, "get_user"):  # returns { data: { user }, error }
            try:
                resp = supabase.auth.get_user(token)
            except Exception:
                # network failure or misconfigured URL
                logger.exception("Failed to resolve  user from token")
                resp = None

            if resp is not None:
                data = getattr(resp, "data", None) or (resp.get("data") if isinstance(resp, dict) else None)
                user = None
                if isinstance(data, dict) and data.get("user"):
                    user = data.get("user")
                elif isinstance(data, dict):
                    user = data
                return user

        # Older clients might have admin.get_user_by_token or auth.api.get_user
        if hasattr(supabase.auth, "get_user_by_token"):
            try:
                resp = supabase.auth.get_user_by_token(token)
                return resp
            except Exception:
                logger.exception("Failed to call get_user_by_token")

        # Last resort: call the REST endpoint for userinfo if service key available
        url = os.getenv("SUPABASE_URL")
        service_key = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if url and service_key:
            try:
                import requests
                headers = {"Authorization": f"Bearer {token}", "apikey": service_key}
                r = requests.get(f"{url}/auth/v1/user", headers=headers, timeout=5)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                logger.exception("Failed to call /auth/v1/user")
    except Exception:
        logger.exception("Unexpected error when resolving user from token")

    # Fallback: try to decode JWT locally (no verification)
    payload = _decode_jwt_unverified(token)
    if payload:
        # common places to find user id: 'sub', 'user_id', 'id'
        uid = payload.get('sub') or payload.get('user_id') or payload.get('id')
        if uid:
            return {"id": uid, **({"email": payload.get('email')} if payload.get('email') else {})}

    return None
