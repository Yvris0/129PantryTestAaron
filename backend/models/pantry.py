"""Pydantic models for the pantry app. Mirrored by hand in frontend/src/lib/types.ts."""

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


MONTH_PATTERN = r"^\d{4}-\d{2}$"

# ---------- auth / meta ----------


class LoginRequest(BaseModel):
    pin: str = Field(min_length=1, max_length=32)


class Me(BaseModel):
    role: Literal["admin"] = "admin"


class Meta(BaseModel):
    current_month: str
    previous_month: str
    today: str
    currency: str = "SGD"
    timezone: str


# ---------- items ----------


class ItemBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    category: str = Field(default="Snacks", max_length=40)
    price: float = Field(ge=0)
    stock: int = Field(default=0, ge=0)
    low_threshold: int = Field(default=5, ge=0)
    shop_url: str = ""
    emoji: str = Field(default="", max_length=8)
    active: bool = True


class ItemCreate(ItemBase):
    pass


class ItemUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    category: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)
    stock: Optional[int] = Field(default=None, ge=0)
    low_threshold: Optional[int] = Field(default=None, ge=0)
    shop_url: Optional[str] = None
    emoji: Optional[str] = None
    active: Optional[bool] = None


class Item(ItemBase):
    id: str = Field(default_factory=new_id)
    created_at: datetime = Field(default_factory=utcnow)


class RestockRequest(BaseModel):
    quantity: int = Field(gt=0, le=10000)


# ---------- customers ----------


class Customer(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    telegram_chat_id: Optional[int] = None
    telegram_username: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class CustomerSummary(BaseModel):
    id: str
    name: str
    has_telegram: bool
    telegram_username: str
    month_total: float
    outstanding: float


class CustomerName(BaseModel):
    id: str
    name: str


# ---------- orders ----------


class OrderLine(BaseModel):
    item_id: str
    quantity: int = Field(gt=0, le=50)


class WebOrderCreate(BaseModel):
    customer_name: str = Field(min_length=1, max_length=60)
    lines: list[OrderLine] = Field(min_length=1)


class Order(BaseModel):
    id: str = Field(default_factory=new_id)
    customer_id: str
    customer_name: str
    item_id: str
    item_name: str
    unit_price: float
    quantity: int
    total: float
    source: Literal["web", "telegram", "admin"]
    month: str
    created_at: datetime = Field(default_factory=utcnow)


class OrderResult(BaseModel):
    orders: list[Order]
    total: float
    month_total: float


# ---------- bills ----------

BillStatus = Literal["open", "unpaid", "pending", "paid"]


class BillLine(BaseModel):
    item_name: str
    quantity: int
    total: float


class Bill(BaseModel):
    customer_id: str
    customer_name: str
    month: str
    total: float
    status: BillStatus
    has_telegram: bool
    sent_at: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    lines: list[BillLine]


class SendBillsRequest(BaseModel):
    month: str = Field(pattern=MONTH_PATTERN)


class SendBillsResult(BaseModel):
    month: str
    billed: int
    telegram_sent: int
    no_telegram: list[str]


class MarkPaidRequest(BaseModel):
    customer_id: str
    month: str = Field(pattern=MONTH_PATTERN)


# ---------- payments ----------


class Payment(BaseModel):
    id: str = Field(default_factory=new_id)
    customer_id: str
    customer_name: str
    month: str
    amount: float
    status: Literal["pending", "approved", "rejected"] = "pending"
    reason: str = ""
    source: Literal["web", "telegram"]
    has_screenshot: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    reviewed_at: Optional[datetime] = None


class RejectRequest(BaseModel):
    reason: str = Field(default="", max_length=200)


# ---------- settings / telegram ----------


class Settings(BaseModel):
    admin_chat_id: Optional[int] = None
    payment_instructions: str = "PayNow to 9123 4567 (Pantry Fund). Please put your name in the reference."


class SettingsUpdate(BaseModel):
    admin_chat_id: Optional[int] = None
    payment_instructions: str = Field(min_length=1, max_length=500)


class TelegramStatus(BaseModel):
    token_configured: bool
    bot_username: str
    webhook_url: str
    pending_updates: int
    last_error: str
    admin_chat_id: Optional[int]


class WebhookSetupRequest(BaseModel):
    base_url: str = Field(min_length=8)


class WebhookSetupResult(BaseModel):
    ok: bool
    webhook_url: str
    description: str


class TgButton(BaseModel):
    text: str
    data: str


class TelegramLogEntry(BaseModel):
    id: str = Field(default_factory=new_id)
    chat_id: int
    direction: Literal["in", "out"]
    text: str
    buttons: list[list[TgButton]] = []
    has_photo: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class SimRequest(BaseModel):
    chat_id: int = Field(ge=1, lt=1000)
    first_name: str = "Sim User"
    text: Optional[str] = None
    callback_data: Optional[str] = None
    label: Optional[str] = None
    photo_base64: Optional[str] = None


# ---------- dashboard ----------


class TopItem(BaseModel):
    name: str
    quantity: int


class DailyPoint(BaseModel):
    date: str
    total: float


class DashboardStats(BaseModel):
    month: str
    month_revenue: float
    month_orders: int
    outstanding: float
    pending_verifications: int
    low_stock_count: int
    customers: int
    telegram_linked: int
    top_items: list[TopItem]
    daily: list[DailyPoint]

