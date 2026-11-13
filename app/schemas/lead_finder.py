from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.lead_finder.constants import COMPANY_TYPES

Short = Annotated[str, Field(max_length=80)]


class KeyIn(BaseModel):
    key: str = Field(min_length=8, max_length=300)

    @field_validator("key")
    @classmethod
    def one_clean_token(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 8 or any(ch.isspace() for ch in v):
            raise ValueError("a key is one piece of text with no spaces")
        return v


class MailboxIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=300)
    from_name: str = Field(default="", max_length=100)
    smtp_host: str = Field(default="smtp.hostinger.com", min_length=3, max_length=255)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    imap_host: str = Field(default="imap.hostinger.com", max_length=255)
    imap_port: int = Field(default=993, ge=1, le=65535)

    @field_validator("email", "smtp_host", "imap_host", "from_name")
    @classmethod
    def trimmed(cls, v: str) -> str:
        return v.strip()


class RecipientIn(BaseModel):
    """One line of a bulk list: the email, and the person's name and company when the list has them"""
    email: str = Field(min_length=3, max_length=254)
    name: str = Field("", max_length=120)
    company: str = Field("", max_length=200)

    @field_validator("email", "name", "company")
    @classmethod
    def trimmed(cls, v: str) -> str:
        return v.strip()


class AgentRunIn(BaseModel):
    """fast / smart: find companies in a place. bulk: email the user's own list with the user's own message."""
    mode: Literal["fast", "smart", "bulk"] = "fast"
    country: str = Field("", max_length=60)
    state: str = Field("", max_length=60)
    city: str = Field("", max_length=80)
    company_type: str = ""
    limit: int = Field(20, ge=5, le=50)
    recipients: list[RecipientIn] = Field(default_factory=list, max_length=1000)
    subject: str = Field("", max_length=200)
    message: str = Field("", max_length=5000)

    @field_validator("country", "state", "city")
    @classmethod
    def trimmed(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def what_the_mode_needs(self):
        if self.mode == "bulk":
            if not self.recipients:
                raise ValueError("add at least one email")
            if not self.subject.strip() or not self.message.strip():
                raise ValueError("write a subject and a message")
        else:
            if len(self.country) < 2 or len(self.city) < 2:
                raise ValueError("pick a country and a city")
            if self.company_type not in COMPANY_TYPES:
                raise ValueError("unknown company type")
            self.recipients = []
        return self


class MailboxModeIn(BaseModel):
    test_mode: bool


class RetryIn(BaseModel):
    website: str = Field("", max_length=255)


class ContactPatch(BaseModel):
    selected: Optional[bool] = None
    personal_line: Optional[str] = Field(None, max_length=600)


class TemplateIn(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    text: str = Field("", max_length=400)
    enabled: bool = True


class SettingsIn(BaseModel):
    roles: dict[str, list[Short]] = Field(default_factory=dict)
    skip_sites: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=100)
    templates: list[TemplateIn] = Field(default_factory=list, max_length=8)
    subject: str = Field("", max_length=200)
    message: str = Field("", max_length=5000)
    daily_limit: int = Field(50, ge=1, le=500)
    at_once: int = Field(2, ge=1, le=5)
    tools: dict[str, bool] = Field(default_factory=dict)
    agent_subject: str = Field("", max_length=200)
    agent_message: str = Field("", max_length=5000)
    emails_per_day: int = Field(30, ge=1, le=200)

    @field_validator("roles")
    @classmethod
    def known_role_groups(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        unknown = [k for k in v if k != "default" and k not in COMPANY_TYPES]
        if unknown:
            raise ValueError(f"unknown company types: {', '.join(unknown)}")
        if any(len(roles) > 20 for roles in v.values()):
            raise ValueError("at most 20 job titles per company type")
        if not v.get("default", ["Owner"]):
            raise ValueError("the list for any other company needs at least one job title")
        return v

    @field_validator("tools")
    @classmethod
    def known_tools(cls, v: dict[str, bool]) -> dict[str, bool]:
        unknown = [k for k in v if k not in ("hunter", "serper", "verifier", "places", "ai", "apollo", "gplaces", "openai", "pdl")]
        if unknown:
            raise ValueError(f"unknown tools: {', '.join(unknown)}")
        return v
