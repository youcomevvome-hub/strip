from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, HttpUrl


# --- auth ---
class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: EmailStr
    is_admin: bool

    class Config:
        from_attributes = True


# --- sources ---
class SourceCreate(BaseModel):
    name: str
    url: HttpUrl
    rss_url: HttpUrl | None = None
    scrape_mode: str = "auto"
    tags: str = ""
    enabled: bool = True


class SourceUpdate(BaseModel):
    name: str | None = None
    url: HttpUrl | None = None
    rss_url: HttpUrl | None = None
    scrape_mode: str | None = None
    tags: str | None = None
    enabled: bool | None = None


class SourceOut(BaseModel):
    id: int
    name: str
    url: str
    rss_url: str | None
    scrape_mode: str
    tags: str
    enabled: bool
    last_scraped_at: datetime | None

    class Config:
        from_attributes = True


# --- articles & posts ---
class ArticleOut(BaseModel):
    id: int
    source_id: int
    url: str
    title: str
    author: str | None
    published_at: datetime | None
    image_url: str | None
    status: str
    fetched_at: datetime

    class Config:
        from_attributes = True


class PostUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    body: str | None = None
    bullets: list[str] | None = None
    hashtags: list[str] | None = None
    links: list[str] | None = None
    image_url: str | None = None
    variants: dict | None = None


class DeliveryOut(BaseModel):
    id: int
    platform: str
    status: str
    external_url: str | None
    error: str | None
    delivered_at: datetime | None

    class Config:
        from_attributes = True


class PostOut(BaseModel):
    id: int
    article_id: int
    title: str
    summary: str
    body: str
    bullets: list
    hashtags: list
    links: list
    image_url: str | None
    variants: dict
    status: str
    created_at: datetime
    updated_at: datetime
    deliveries: list[DeliveryOut] = []

    class Config:
        from_attributes = True


class PublishRequest(BaseModel):
    platforms: list[str]
