from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.services.lead_finder.constants import COMPANY_TYPES, SIZES, TRADES

Short = Annotated[str, Field(max_length=80)]
Code = Annotated[str, Field(pattern=r"^\d{2,6}$")]


class CompanyIn(BaseModel):
    """One company from the form, a CSV row or a search result. Only name, country and city are required."""
    name: str = Field(min_length=1, max_length=200)
    website: str = Field("", max_length=255)
    country: str = Field(min_length=1, max_length=80)
    state: str = Field("", max_length=80)
    city: str = Field(min_length=1, max_length=120)
    size: str = ""
    company_type: str = ""
    trades: list[str] = Field(default_factory=list, max_length=len(TRADES))
    contact_roles: list[Short] = Field(default_factory=list, max_length=20)
    sectors: list[Short] = Field(default_factory=list, max_length=20)
    project_types: list[Short] = Field(default_factory=list, max_length=20)
    revenue: str = Field("", max_length=40)
    project_size: str = Field("", max_length=40)
    founded_year: Optional[int] = Field(None, ge=1800, le=2100)
    service_area: str = Field("", max_length=200)
    software: list[Short] = Field(default_factory=list, max_length=30)
    naics: list[Code] = Field(default_factory=list, max_length=20)
    license_number: str = Field("", max_length=80)
    credentials: list[Short] = Field(default_factory=list, max_length=20)
    priority: Literal["", "high", "medium", "low"] = ""
    tags: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=20)
    found_via: str = Field("", max_length=80)
    notes: str = Field("", max_length=2000)

    @field_validator("name", "country", "city")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("can't be empty")
        return v.strip()

    @field_validator("founded_year", mode="before")
    @classmethod
    def empty_year(cls, v):
        return None if v in ("", None) else v

    @field_validator("size")
    @classmethod
    def known_size(cls, v: str) -> str:
        if v and v not in SIZES:
            raise ValueError(f"must be one of {', '.join(SIZES)}")
        return v

    @field_validator("company_type")
    @classmethod
    def known_type(cls, v: str) -> str:
        if v and v not in COMPANY_TYPES:
            raise ValueError(f"must be one of {', '.join(COMPANY_TYPES)}")
        return v

    @field_validator("trades")
    @classmethod
    def known_trades(cls, v: list[str]) -> list[str]:
        unknown = [t for t in v if t not in TRADES]
        if unknown:
            raise ValueError(f"unknown trades: {', '.join(unknown)}")
        return v


class CompaniesIn(BaseModel):
    companies: list[CompanyIn] = Field(min_length=1, max_length=500)
    source: Literal["manual", "search", "csv"] = "manual"
    search_id: Optional[str] = Field(None, max_length=64)  # the search that found them


class SearchIn(BaseModel):
    """What was picked in "Find companies"."""
    countries: list[Short] = Field(default_factory=list, max_length=6)
    states: list[Short] = Field(default_factory=list, max_length=20)
    cities: list[Short] = Field(default_factory=list, max_length=10)
    company_types: list[str] = Field(default_factory=list, max_length=len(COMPANY_TYPES))
    trades: list[str] = Field(default_factory=list, max_length=len(TRADES))
    sizes: list[str] = Field(default_factory=list, max_length=len(SIZES))
    ownership: Literal["", "Private", "Partnership", "Public"] = ""
    keywords: list[Short] = Field(default_factory=list, max_length=15)
    keyword_match: Literal["any", "all"] = "any"
    exclude_in_list: bool = True
    exclude_domains: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=50)
    limit: int = Field(25, ge=1, le=100)


class KeyIn(BaseModel):
    key: str = Field(min_length=8, max_length=300)

    @field_validator("key")
    @classmethod
    def one_clean_token(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 8 or any(ch.isspace() for ch in v):
            raise ValueError("a key is one piece of text with no spaces")
        return v


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
