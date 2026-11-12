"""Core pantry logic shared by the web routes, the Telegram bot and the month-end cron."""

import html
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from lib import telegram
from lib.db import db
from models.pantry import Bill, BillLine, Customer, Order, Payment, Settings, SendBillsResult


class PantryError(ValueError):
    """User-facing business rule violation (out of stock, unknown item...)."""


def tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("APP_TZ", "Asia/Singapore"))


def month_of(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz()).strftime("%Y-%m")


def current_month() -> str:
    return month_of(datetime.now(timezone.utc))


def previous_month() -> str:
    first = datetime.now(tz()).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def month_label(month: str) -> str:
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def money(v: float) -> str:
    return f"${v:,.2f}"


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


# ---------- settings ----------


async def get_settings() -> Settings:
    doc = await db.settings.find_one({"key": "main"})
    return Settings(**(doc or {}))


async def save_settings(data: dict) -> Settings:
    await db.settings.update_one({"key": "main"}, {"$set": data}, upsert=True)
    return await get_settings()


async def notify_admin(text: str) -> None:
    settings = await get_settings()
    if settings.admin_chat_id:
        await telegram.send(settings.admin_chat_id, text)


# ---------- customers ----------


def clean_name(name: str) -> str:
    return " ".join(name.split())


async def get_or_create_customer(name: str) -> dict:
    name = clean_name(name)
    if not name:
        raise PantryError("Name is required")
    doc = await db.customers.find_one({"name_lower": name.lower()}, {"_id": 0})
    if doc:
        return doc
    customer = Customer(name=name).model_dump()
    customer["name_lower"] = name.lower()
    await db.customers.insert_one(customer)
    customer.pop("_id", None)
    return customer


async def link_telegram(customer_id: str, chat_id: int, username: str) -> None:
    # One chat can only belong to one customer.
    await db.customers.update_many({"telegram_chat_id": chat_id}, {"$set": {"telegram_chat_id": None}})
    await db.customers.update_one(
        {"id": customer_id}, {"$set": {"telegram_chat_id": chat_id, "telegram_username": username}}
    )


# ---------- stock + orders ----------


async def _maybe_alert_low_stock(item: dict) -> None:
    if item["stock"] > item["low_threshold"] or item.get("low_alert_sent"):
        return
    await db.items.update_one({"id": item["id"]}, {"$set": {"low_alert_sent": True}})
    link = f"\nRestock: {html.escape(item['shop_url'])}" if item.get("shop_url") else ""
    await notify_admin(
        f"⚠️ <b>Low stock</b>: {html.escape(item['name'])} — {item['stock']} left "
        f"(threshold {item['low_threshold']}).{link}"
    )


async def place_order(customer: dict, lines: list[tuple[str, int]], source: str) -> tuple[list[Order], float, float]:
    merged: dict[str, int] = defaultdict(int)
    for item_id, qty in lines:
        merged[item_id] += qty

    items: dict[str, dict] = {}
    for item_id, qty in merged.items():
        item = await db.items.find_one({"id": item_id, "active": True}, {"_id": 0})
        if not item:
            raise PantryError("Item not found")
        if item["stock"] < qty:
            raise PantryError(f"Only {item['stock']} {item['name']} left in stock")
        items[item_id] = item

    month = current_month()
    orders: list[Order] = []
    for item_id, qty in merged.items():
        updated = await db.items.find_one_and_update(
            {"id": item_id, "stock": {"$gte": qty}}, {"$inc": {"stock": -qty}}, projection={"_id": 0}, return_document=True
        )
        if not updated:
            raise PantryError(f"{items[item_id]['name']} just ran out")
        item = items[item_id]
        orders.append(
            Order(
                customer_id=customer["id"],
                customer_name=customer["name"],
                item_id=item_id,
                item_name=item["name"],
                unit_price=item["price"],
                quantity=qty,
                total=round(item["price"] * qty, 2),
                source=source,  # type: ignore[arg-type]
                month=month,
            )
        )
        await _maybe_alert_low_stock(updated)

    await db.orders.insert_many([o.model_dump() for o in orders])
    total = round(sum(o.total for o in orders), 2)
    month_total = await customer_month_total(customer["id"], month)
    return orders, total, month_total


async def restock(item_id: str, quantity: int) -> Optional[dict]:
    item = await db.items.find_one_and_update(
        {"id": item_id}, {"$inc": {"stock": quantity}}, projection={"_id": 0}, return_document=True
    )
    if item and item["stock"] > item["low_threshold"]:
        await db.items.update_one({"id": item_id}, {"$set": {"low_alert_sent": False}})
    return item


async def customer_month_total(customer_id: str, month: str) -> float:
    orders = await db.orders.find({"customer_id": customer_id, "month": month}, {"total": 1}).to_list(5000)
    return round(sum(o["total"] for o in orders), 2)


# ---------- bills ----------


async def build_bills(month: str, customer_id: Optional[str] = None) -> list[Bill]:
    query: dict = {"month": month}
    if customer_id:
        query["customer_id"] = customer_id
    orders = await db.orders.find(query, {"_id": 0}).to_list(20000)
    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for o in orders:
        line = grouped[o["customer_id"]][o["item_name"]]
        line[0] += o["quantity"]
        line[1] += o["total"]

    customers = {c["id"]: c for c in await db.customers.find({"id": {"$in": list(grouped)}}, {"_id": 0}).to_list(5000)}
    states = {b["customer_id"]: b for b in await db.bills.find(query, {"_id": 0}).to_list(5000)}
    bills: list[Bill] = []
    for cid, lines in grouped.items():
        cust = customers.get(cid, {"name": "Unknown"})
        state = states.get(cid, {})
        bills.append(
            Bill(
                customer_id=cid,
                customer_name=cust["name"],
                month=month,
                total=round(sum(v[1] for v in lines.values()), 2),
                status=state.get("status", "open"),
                has_telegram=bool(cust.get("telegram_chat_id")),
                sent_at=aware(state.get("sent_at")),
                paid_at=aware(state.get("paid_at")),
                lines=[BillLine(item_name=n, quantity=v[0], total=round(v[1], 2)) for n, v in sorted(lines.items())],
            )
        )
    bills.sort(key=lambda b: b.customer_name.lower())
    return bills


async def set_bill_status(customer_id: str, month: str, status: str, extra: Optional[dict] = None) -> None:
    await db.bills.update_one(
        {"customer_id": customer_id, "month": month},
        {"$set": {"status": status, **(extra or {})}, "$setOnInsert": {"customer_id": customer_id, "month": month}},
        upsert=True,
    )


def bill_text(bill: Bill, intro: str, instructions: str) -> str:
    rows = "\n".join(f"• {l.quantity} × {html.escape(l.item_name)} — {money(l.total)}" for l in bill.lines)
    return (
        f"{intro}\n\n🧾 <b>Pantry bill · {month_label(bill.month)}</b>\n{rows}\n\n"
        f"<b>Total: {money(bill.total)}</b>\n\n{html.escape(instructions)}\n\n"
        "Once paid, tap <b>I've paid</b> and send a screenshot of the transfer."
    )


async def send_bills(month: str) -> SendBillsResult:
    settings = await get_settings()
    bills = await build_bills(month)
    billed = sent = 0
    no_telegram: list[str] = []
    for bill in bills:
        if bill.status in ("paid", "pending") or bill.total <= 0:
            continue
        billed += 1
        await set_bill_status(bill.customer_id, month, "unpaid", {"sent_at": datetime.now(timezone.utc)})
        cust = await db.customers.find_one({"id": bill.customer_id}, {"telegram_chat_id": 1})
        chat_id = (cust or {}).get("telegram_chat_id")
        if not chat_id:
            no_telegram.append(bill.customer_name)
            continue
        intro = f"Hi {html.escape(bill.customer_name)} 👋 Here's your pantry bill for last month."
        ok = await telegram.send(
            chat_id, bill_text(bill, intro, settings.payment_instructions), [[("💳 I've paid", f"paid:{month}")]]
        )
        sent += int(ok)
    return SendBillsResult(month=month, billed=billed, telegram_sent=sent, no_telegram=no_telegram)


async def amount_due(customer_id: str) -> Optional[tuple[str, float]]:
    """Oldest sent-but-unpaid bill, else the current month's open tab."""
    unpaid = await db.bills.find({"customer_id": customer_id, "status": "unpaid"}).sort("month", 1).to_list(50)
    for doc in unpaid:
        total = await customer_month_total(customer_id, doc["month"])
        if total > 0:
            return doc["month"], total
    month = current_month()
    state = await db.bills.find_one({"customer_id": customer_id, "month": month})
    total = await customer_month_total(customer_id, month)
    if total > 0 and (not state or state.get("status") in ("open", "unpaid")):
        return month, total
    return None


# ---------- payments ----------


async def submit_payment(customer: dict, month: str, image: bytes, content_type: str, source: str) -> Payment:
    amount = await customer_month_total(customer["id"], month)
    payment = Payment(
        customer_id=customer["id"], customer_name=customer["name"], month=month, amount=amount, source=source  # type: ignore[arg-type]
    )
    await db.payments.insert_one({**payment.model_dump(), "screenshot": image, "content_type": content_type})
    await set_bill_status(customer["id"], month, "pending")
    await notify_admin(
        f"💳 <b>{html.escape(customer['name'])}</b> submitted a payment of {money(amount)} "
        f"for {month_label(month)}. Review it in the dashboard."
    )
    return payment


async def review_payment(payment_id: str, approve: bool, reason: str = "") -> Optional[Payment]:
    doc = await db.payments.find_one({"id": payment_id}, {"_id": 0, "screenshot": 0})
    if not doc:
        return None
    if doc["status"] != "pending":
        raise PantryError("Payment already reviewed")
    now = datetime.now(timezone.utc)
    status = "approved" if approve else "rejected"
    await db.payments.update_one({"id": payment_id}, {"$set": {"status": status, "reason": reason, "reviewed_at": now}})
    if approve:
        await set_bill_status(doc["customer_id"], doc["month"], "paid", {"paid_at": now})
    else:
        await set_bill_status(doc["customer_id"], doc["month"], "unpaid")

    cust = await db.customers.find_one({"id": doc["customer_id"]}, {"telegram_chat_id": 1})
    chat_id = (cust or {}).get("telegram_chat_id")
    label = month_label(doc["month"])
    if approve:
        await telegram.send(chat_id, f"✅ Your payment of {money(doc['amount'])} for {label} is confirmed. Thank you!")
    else:
        why = f"\nReason: {html.escape(reason)}" if reason else ""
        await telegram.send(
            chat_id,
            f"❌ Your payment screenshot for {label} could not be verified.{why}\nPlease send a new one.",
            [[("💳 I've paid", f"paid:{doc['month']}")]],
        )
    doc.update(status=status, reason=reason, reviewed_at=now)
    return Payment(**doc)
