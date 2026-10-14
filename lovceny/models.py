from datetime import datetime, timezone
from sqlalchemy import String, Integer, Numeric, Boolean, DateTime, ForeignKey, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


def now():
    return datetime.now(timezone.utc)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    slug: Mapped[str] = mapped_column(String(300), index=True)
    ean: Mapped[str | None] = mapped_column(String(32), index=True)
    image_url: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(200))
    # slug z lovceny/categories.py (SEO stránky /kategorie/…); None = nezařazeno
    category_slug: Mapped[str | None] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    offers: Mapped[list["Offer"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    watches: Mapped[list["Watch"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class Offer(Base):
    """Jeden produkt v jednom obchodě."""
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    shop: Mapped[str] = mapped_column(String(40), index=True)  # slug z registry
    url: Mapped[str] = mapped_column(Text, unique=True)
    external_id: Mapped[str | None] = mapped_column(String(100))  # ITEM_ID z feedu
    price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    original_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    in_stock: Mapped[bool | None] = mapped_column(Boolean)
    # ok | pending (cena bude doplněna) | blocked | error
    status: Mapped[str] = mapped_column(String(20), default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    product: Mapped[Product] = relationship(back_populates="offers")
    points: Mapped[list["PricePoint"]] = relationship(back_populates="offer", cascade="all, delete-orphan",
                                                      order_by="PricePoint.captured_at")


class PricePoint(Base):
    __tablename__ = "price_points"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"))
    price: Mapped[float] = mapped_column(Numeric(12, 2))
    original_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    in_stock: Mapped[bool | None] = mapped_column(Boolean)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="scrape")  # scrape | feed | demo
    offer: Mapped[Offer] = relationship(back_populates="points")
    __table_args__ = (Index("ix_pp_offer_time", "offer_id", "captured_at"),)


class Watch(Base):
    """Hlídání ceny (e-mail + cílová cena)."""
    __tablename__ = "watches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    target_price: Mapped[float] = mapped_column(Numeric(12, 2))
    only_in_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # cena, o které jsme naposledy psali; None = zatím nic / cena se mezitím vrátila nad cíl
    notified_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    product: Mapped[Product] = relationship(back_populates="watches")


class PushSubscription(Base):
    """Web Push odběr prohlížeče pro hlídání ceny (alternativa k e-mailu). Bez e-mailu, jen endpoint + klíče."""
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    target_price: Mapped[float] = mapped_column(Numeric(12, 2))
    only_in_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    token: Mapped[str] = mapped_column(String(64), unique=True)  # odhlášení bez účtu
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    product: Mapped[Product] = relationship()
    __table_args__ = (Index("ix_push_endpoint", "endpoint"),)


class TrackRequest(Base):
    """Záznam o přidání produktu ze živého hledání (rate limit podle IP hashe)."""
    __tablename__ = "track_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)


class ShopRunStat(Base):
    """Výsledek jednoho běhu stahovače pro jeden obchod (monitoring blokací, /stav)."""
    __tablename__ = "shop_run_stats"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(40), index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    shop: Mapped[str] = mapped_column(String(40), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)   # HTTP 403/429/503
    captcha: Mapped[int] = mapped_column(Integer, default=0)   # JS challenge / captcha stránka
    robots: Mapped[int] = mapped_column(Integer, default=0)    # robots.txt nepovoluje
    errors: Mapped[int] = mapped_column(Integer, default=0)    # síť, 404, cena nenalezena…
    skipped: Mapped[int] = mapped_column(Integer, default=0)   # nedávno kontrolované
    statuses: Mapped[str | None] = mapped_column(Text)         # JSON {"200": 5, "403": 2}
    last_error: Mapped[str | None] = mapped_column(Text)

    @property
    def failures(self) -> int:
        return self.blocked + self.captcha + self.robots + self.errors


class AnalyticsEvent(Base):
    """Anonymní analytika bez cookies: cesta, doména odkazujícího webu, normalizovaný dotaz.
    visitor = hash(denní sůl + IP + UA); po 24 h ho mažeme (NULL) a sůl zahazujeme."""
    __tablename__ = "analytics_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)          # YYYY-MM-DD (Europe/Prague)
    kind: Mapped[str] = mapped_column(String(10), default="view")     # view | search | live
    path: Mapped[str] = mapped_column(String(200))
    ref_domain: Mapped[str | None] = mapped_column(String(100))
    query: Mapped[str | None] = mapped_column(String(80))
    results: Mapped[int | None] = mapped_column(Integer)
    product_id: Mapped[int | None] = mapped_column(Integer, index=True)
    visitor: Mapped[str | None] = mapped_column(String(32))


class AnalyticsDaily(Base):
    """Denní souhrn (počítá se z událostí před smazáním otisků)."""
    __tablename__ = "analytics_daily"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    views: Mapped[int] = mapped_column(Integer, default=0)
    uniques: Mapped[int] = mapped_column(Integer, default=0)


class AnalyticsSalt(Base):
    __tablename__ = "analytics_salts"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    salt: Mapped[str] = mapped_column(String(64))


class ContactMessage(Base):
    """Zpráva z kontaktního formuláře /o-nas (čte se v /admin)."""
    __tablename__ = "contact_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    name: Mapped[str | None] = mapped_column(String(100))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    topic: Mapped[str] = mapped_column(String(40), default="jine")
    message: Mapped[str] = mapped_column(Text)


class PushTopicSubscription(Base):
    """Web Push odběr tématu (zatím jen „tydenni“ = Týdenní přehled slev). Oddělené od hlídání cen produktů."""
    __tablename__ = "push_topic_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(30), index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_push_topic_endpoint", "topic", "endpoint"),)


class WeeklyDigest(Base):
    """Snímek Týdenního přehledu slev (/prehled/<rok>-<týden>). Položky jako JSON, aby stránka zůstala stejná."""
    __tablename__ = "weekly_digests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    items: Mapped[str] = mapped_column(Text, default="[]")
    push_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    push_sent: Mapped[int] = mapped_column(Integer, default=0)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    email_sent: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (Index("ux_digest_week", "year", "week", unique=True),)


class DigestEmail(Base):
    """E-mailová verze Týdenního přehledu (double opt-in). Aktivní jen s nastaveným EMAIL_BACKEND."""
    __tablename__ = "digest_emails"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BudgetWatch(Base):
    """v7 – Hlídání podle rozpočtu: kategorie (+ volitelně klíčové slovo / značka) a maximální cena.
    Kanál „push“ (endpoint prohlížeče, bez e-mailu) nebo „email“ (double opt-in; posílá se jen s EMAIL_BACKEND)."""
    __tablename__ = "budget_watches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)       # správa / zrušení bez účtu
    category_slug: Mapped[str] = mapped_column(String(40), index=True)
    keyword: Mapped[str | None] = mapped_column(String(60))           # např. „bosch“, „65“, „oled“
    max_price: Mapped[float] = mapped_column(Numeric(12, 2))
    only_in_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    channel: Mapped[str] = mapped_column(String(10), default="push")  # push | email
    endpoint: Mapped[str | None] = mapped_column(Text)
    p256dh: Mapped[str | None] = mapped_column(String(200))
    auth: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)   # push = hned True
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_day: Mapped[str | None] = mapped_column(String(10))           # YYYY-MM-DD (Praha) – denní strop
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    hits: Mapped[list["BudgetHit"]] = relationship(back_populates="budget", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_budget_endpoint", "endpoint"),)


class BudgetHit(Base):
    """Co jsme k danému hlídání rozpočtu už oznámili (proti duplicitám): znovu jen při dalším poklesu ceny."""
    __tablename__ = "budget_hits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budget_watches.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    price: Mapped[float] = mapped_column(Numeric(12, 2))
    notified: Mapped[bool] = mapped_column(Boolean, default=True)     # False = jen výchozí stav při založení
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    budget: Mapped[BudgetWatch] = relationship(back_populates="hits")
    __table_args__ = (Index("ux_budget_hit", "budget_id", "product_id", unique=True),)


class TopicSend(Base):
    """Jednorázová rozesílka tématu (např. Black Friday přehled v daný den) – idempotence napříč běhy."""
    __tablename__ = "topic_sends"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)          # např. „black-friday:2026-11-27“
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[str | None] = mapped_column(Text)                    # JSON snímek položek
