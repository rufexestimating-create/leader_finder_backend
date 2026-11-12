"""Lead Finder API. Companies are looked into in the background (services/lead_finder/runner.py); the dashboard
polls GET /lead-finder/companies to watch the steps finish."""
from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import get_current_user
import httpx

from app.schemas.lead_finder import CompaniesIn, ContactPatch, KeyIn, RetryIn, SearchIn, SettingsIn
from app.services.lead_finder import discover, keys, runner, store
from app.services.lead_finder.providers import Hunter, ProviderError

router = APIRouter(dependencies=[Depends(get_current_user)])


def _not_found(what: str = "Company") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.")


@router.get("/companies", summary="Every company with its people and progress, newest first")
def list_companies(user: dict = Depends(get_current_user)):
    return store.list_companies(user["id"])


@router.post("/companies", status_code=status.HTTP_201_CREATED, summary="Add companies (form, CSV or search); they start in the background")
def add_companies(data: CompaniesIn, user: dict = Depends(get_current_user)):
    return store.create_companies(user["id"], [c.model_dump() for c in data.companies], data.source, data.search_id)


@router.post("/search", summary="Find companies by filters (Hunter Discover, free); nothing is looked up until you pick some")
def search_companies(data: SearchIn, user: dict = Depends(get_current_user)):
    hunter_key = (keys.load(user["id"]).get("hunter") or {}).get("key")
    if not hunter_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Finding companies needs your Hunter key. Add it in Settings, under Tools.")
    if store.get_settings(user["id"]).get("tools", {}).get("hunter") is False:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Hunter is switched off in Settings, so companies can't be searched.")
    filters = data.model_dump()
    try:
        with httpx.Client() as client:
            found = discover.search(Hunter(hunter_key, client), filters, store.known_domains(user["id"]))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    except ProviderError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    found["search_id"] = store.save_search(user["id"], filters, len(found["results"]))
    return found


@router.get("/companies/{company_id}", summary="One company with its people and progress")
def get_company(company_id: str, user: dict = Depends(get_current_user)):
    company = store.get_company(user["id"], company_id)
    if not company:
        raise _not_found()
    return company


@router.post("/companies/{company_id}/retry", summary="Look into a company again, optionally with its website")
def retry_company(company_id: str, data: RetryIn, user: dict = Depends(get_current_user)):
    company = store.retry_company(user["id"], company_id, data.website)
    if not company:
        raise _not_found()
    return company


@router.post("/companies/{company_id}/approve", summary="Approve the picked people for a campaign")
def approve_company(company_id: str, user: dict = Depends(get_current_user)):
    company = store.approve_company(user["id"], company_id)
    if not company:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a company that is ready to review can be approved.")
    return company


@router.delete("/companies/{company_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a company and its people")
def delete_company(company_id: str, user: dict = Depends(get_current_user)):
    if not store.delete_company(user["id"], company_id):
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/contacts/{contact_id}", summary="Pick a person for the campaign, or change their personal line")
def update_contact(contact_id: str, data: ContactPatch, user: dict = Depends(get_current_user)):
    patch = data.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to change.")
    contact = store.update_contact(user["id"], contact_id, patch)
    if not contact:
        raise _not_found("Person")
    return contact


@router.get("/settings", summary="Lead Finder settings, and which tools this user has connected")
def get_settings(user: dict = Depends(get_current_user)):
    s = store.get_settings(user["id"])
    return {**s, "tool_status": runner.tools_status(s, keys.load(user["id"]))}


@router.put("/settings", summary="Save Lead Finder settings")
def save_settings(data: SettingsIn, user: dict = Depends(get_current_user)):
    payload = data.model_dump()
    payload["templates"] = [t for t in payload["templates"] if t["text"].strip()]
    s = store.save_settings(user["id"], payload)
    return {**s, "tool_status": runner.tools_status(s, keys.load(user["id"]))}


# ---------- The user's own tool keys ----------

def _provider(name: str) -> str:
    if name not in keys.PROVIDERS:
        raise _not_found("Tool")
    return name


def _unavailable(e: keys.KeysUnavailable) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e))


@router.get("/keys", summary="Which tools this user has connected (only the last four characters of each key)")
def key_status(user: dict = Depends(get_current_user)):
    try:
        return keys.status(user["id"])
    except keys.KeysUnavailable as e:
        raise _unavailable(e)


@router.put("/keys/{provider}", summary="Check a key against its tool and, if it works, save it (encrypted)")
def save_key(provider: str, data: KeyIn, user: dict = Depends(get_current_user)):
    provider = _provider(provider)
    result = keys.check_key(provider, data.key)
    if not result.ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.message)
    try:
        last4 = keys.save(user["id"], provider, data.key)
    except keys.KeysUnavailable as e:
        raise _unavailable(e)
    return {"provider": provider, "connected": True, "last4": last4, "message": result.message}


@router.post("/keys/{provider}/test", summary="Check the saved key again and say how many credits are left")
def test_key(provider: str, user: dict = Depends(get_current_user)):
    provider = _provider(provider)
    saved = keys.load(user["id"]).get(provider)
    if not saved:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No key is saved for this tool yet.")
    result = keys.check_key(provider, saved["key"])
    return {"ok": result.ok, "message": result.message}


@router.delete("/keys/{provider}", status_code=status.HTTP_204_NO_CONTENT, summary="Remove a saved key")
def delete_key(provider: str, user: dict = Depends(get_current_user)):
    provider = _provider(provider)
    try:
        keys.remove(user["id"], provider)
    except keys.KeysUnavailable as e:
        raise _unavailable(e)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
