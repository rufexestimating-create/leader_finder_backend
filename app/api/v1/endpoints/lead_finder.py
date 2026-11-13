"""Lead Finder API. Companies are looked into in the background (services/lead_finder/runner.py); the dashboard
polls GET /lead-finder/companies to watch the steps finish."""
from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import get_current_user
from app.schemas.lead_finder import AgentRunIn, ContactPatch, KeyIn, MailboxIn, MailboxModeIn, RetryIn, SettingsIn
from app.services.lead_finder import agent, keys, mailbox, runner, store

router = APIRouter(dependencies=[Depends(get_current_user)])


def _not_found(what: str = "Company") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.")


@router.get("/companies", summary="Every company with its people and progress, newest first")
def list_companies(user: dict = Depends(get_current_user)):
    return store.list_companies(user["id"])


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


@router.post("/companies/{company_id}/send", summary="Email the picked people from the user's own mailbox")
def send_company(company_id: str, user: dict = Depends(get_current_user)):
    company = store.get_company(user["id"], company_id)
    if not company:
        raise _not_found()
    if company["status"] not in ("ready", "approved"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a company that is ready to review can be emailed.")
    mb = _mailbox(user)
    test = mailbox.is_test(mb)
    todo = [p for p in company["contacts"]
            if p.get("selected") and p.get("email") and (test or not p.get("sent_at")) and p.get("email_status") != "invalid"]
    if not todo:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Pick at least one person who hasn't been emailed yet.")
    s = store.get_settings(user["id"])
    if test:
        # Everything goes to the user's own inbox; the real people are untouched and can still be emailed later
        msgs = []
        for p in todo:
            subject, body = mailbox.render(s, company, p)
            note = f"TEST MODE: with test mode off, this email goes to {p['name']} <{p['email']}>.\n\n"
            msgs.append(mailbox.build(mb, mb["email"], mb.get("from_name") or "", f"[Test] {subject}", note + body))
        try:
            errors = mailbox.send(mb, msgs, copy_to_sent=False)
        except mailbox.MailboxError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
        if errors[0] is not None and all(errors):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, errors[0])
        return {**company, "test_sent": sum(err is None for err in errors)}
    msgs = [mailbox.build(mb, p["email"], p["name"], *mailbox.render(s, company, p)) for p in todo]
    try:
        errors = mailbox.send(mb, msgs)
    except mailbox.MailboxError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    stamp = store.now_iso()
    for p, msg, err in zip(todo, msgs, errors):
        patch = {"send_error": err} if err else {"sent_at": stamp, "send_error": None, "message_id": msg["Message-ID"], "approved_at": stamp}
        store.update_contact(user["id"], p["id"], patch)
    if any(err is None for err in errors):
        store.update_company(company_id, {"status": "approved"})
    return store.get_company(user["id"], company_id)


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


# ---------- ReachAgent's automatic runs ----------

@router.post("/agent/runs", status_code=status.HTTP_201_CREATED, summary="Start an agent run: find, read, find emails, send")
def start_agent_run(data: AgentRunIn, user: dict = Depends(get_current_user)):
    _mailbox(user)
    if data.mode == "smart":
        s = store.get_settings(user["id"])
        hunter = runner.tools_status(s, keys.load(user["id"]))["hunter"]
        if not (hunter["connected"] and hunter["on"]):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "With enrichment needs your Hunter key: add it in Settings, under Connect your tools.")
    active = [r for r in agent.list_runs(user["id"], 5) if r["status"] in ("queued", "running")]
    if active:
        raise HTTPException(status.HTTP_409_CONFLICT, "A run is already finding companies. Wait for it to finish or stop it.")
    return agent.start(user["id"], data.model_dump())


@router.get("/agent/runs", summary="The latest agent runs, newest first")
def list_agent_runs(user: dict = Depends(get_current_user)):
    return agent.list_runs(user["id"])


@router.get("/agent/runs/{run_id}", summary="One agent run with its companies and progress")
def get_agent_run(run_id: str, user: dict = Depends(get_current_user)):
    run = agent.get(user["id"], run_id)
    if not run:
        raise _not_found("Run")
    return run


@router.post("/agent/runs/{run_id}/stop", summary="Stop an agent run")
def stop_agent_run(run_id: str, user: dict = Depends(get_current_user)):
    if not agent.stop(user["id"], run_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This run has already finished.")
    return agent.get(user["id"], run_id)


# ---------- The user's own mailbox ----------

def _mailbox(user: dict) -> dict:
    try:
        mb = mailbox.load(user["id"])
    except keys.KeysUnavailable as e:
        raise _unavailable(e)
    if not mb:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Connect your mailbox first: Settings, under Your mailbox.")
    return mb


@router.get("/mailbox", summary="The mailbox emails are sent from (never its password)")
def mailbox_status(user: dict = Depends(get_current_user)):
    try:
        return mailbox.status(user["id"])
    except keys.KeysUnavailable as e:
        raise _unavailable(e)


@router.put("/mailbox", summary="Check the mailbox login and, if it works, save it (password encrypted)")
def save_mailbox(data: MailboxIn, user: dict = Depends(get_current_user)):
    mb = data.model_dump()
    result = mailbox.check(mb)
    if not result.ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result.message)
    try:
        mailbox.save(user["id"], mb)
        return {**mailbox.status(user["id"]), "message": result.message}
    except keys.KeysUnavailable as e:
        raise _unavailable(e)


@router.post("/mailbox/test", summary="Send a test email to the mailbox itself")
def test_mailbox(user: dict = Depends(get_current_user)):
    mb = _mailbox(user)
    body = ("This is a test email from ReachAgent.\n\nIf you can read this in your inbox, sending works. "
            "Reply to it and the reply arrives here too, just like a reply from a company you emailed.")
    msg = mailbox.build(mb, mb["email"], mb.get("from_name") or "", "ReachAgent test email", body)
    try:
        error = mailbox.send(mb, [msg])[0]
    except mailbox.MailboxError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    if error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, error)
    return {"message": f"Test email sent to {mb['email']}. Check its inbox, and its Sent folder for the copy."}


@router.patch("/mailbox", summary="Turn test mode on (every email goes to the mailbox itself) or off")
def set_mailbox_mode(data: MailboxModeIn, user: dict = Depends(get_current_user)):
    _mailbox(user)
    try:
        mailbox.set_test_mode(user["id"], data.test_mode)
        return mailbox.status(user["id"])
    except keys.KeysUnavailable as e:
        raise _unavailable(e)


@router.delete("/mailbox", status_code=status.HTTP_204_NO_CONTENT, summary="Disconnect the mailbox")
def delete_mailbox(user: dict = Depends(get_current_user)):
    try:
        mailbox.remove(user["id"])
    except keys.KeysUnavailable as e:
        raise _unavailable(e)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
