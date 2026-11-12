"""Telegram conversation logic: register name → order → bill → I've paid + screenshot."""

import base64
import html
import logging
from typing import Optional

from lib import telegram
from lib.auth import check_pin
from lib.db import db
from lib.pantry import (
    PantryError,
    amount_due,
    build_bills,
    current_month,
    get_or_create_customer,
    get_settings,
    link_telegram,
    money,
    month_label,
    place_order,
    save_settings,
    submit_payment,
)

logger = logging.getLogger(__name__)

MENU = [[("🛒 Order", "order"), ("🧾 My bill", "bill")], [("💳 I've paid", "paid")]]
HELP = (
    "Commands:\n/order — record pantry items\n/bill — this month's tab\n/paid — submit a payment screenshot\n"
    "/menu — main menu"
)


async def _state(chat_id: int) -> dict:
    return await db.tg_sessions.find_one({"chat_id": chat_id}) or {}


async def _set_state(chat_id: int, state: str, data: Optional[dict] = None) -> None:
    await db.tg_sessions.update_one(
        {"chat_id": chat_id}, {"$set": {"state": state, "data": data or {}}}, upsert=True
    )


async def _menu(chat_id: int, name: str, prefix: str = "") -> None:
    await telegram.send(chat_id, f"{prefix}What would you like to do, {html.escape(name)}?", MENU)


async def _items_keyboard(chat_id: int) -> None:
    items = await db.items.find({"active": True, "stock": {"$gt": 0}}, {"_id": 0}).sort("name", 1).to_list(200)
    if not items:
        await telegram.send(chat_id, "The pantry is empty right now 😢", MENU)
        return
    buttons = [(f"{i.get('emoji') or '•'} {i['name']} · {money(i['price'])}", f"item:{i['id']}") for i in items]
    rows = [buttons[n : n + 2] for n in range(0, len(buttons), 2)]
    rows.append([("↩ Menu", "menu")])
    await telegram.send(chat_id, "What did you take from the pantry?", rows)


async def _ask_quantity(chat_id: int, item_id: str) -> None:
    item = await db.items.find_one({"id": item_id, "active": True})
    if not item:
        await telegram.send(chat_id, "That item is no longer available.", MENU)
        return
    top = min(item["stock"], 5)
    qty = [(str(n), f"qty:{item_id}:{n}") for n in range(1, top + 1)]
    await telegram.send(
        chat_id,
        f"How many <b>{html.escape(item['name'])}</b>? ({money(item['price'])} each, {item['stock']} left)",
        [qty, [("↩ Back", "order")]],
    )


async def _record(chat_id: int, customer: dict, item_id: str, qty: int) -> None:
    try:
        orders, total, month_total = await place_order(customer, [(item_id, qty)], "telegram")
    except PantryError as exc:
        await telegram.send(chat_id, f"⚠️ {html.escape(str(exc))}", MENU)
        return
    o = orders[0]
    await telegram.send(
        chat_id,
        f"✅ Recorded {o.quantity} × {html.escape(o.item_name)} ({money(total)}).\n"
        f"Your {month_label(o.month)} tab: <b>{money(month_total)}</b>",
        [[("🛒 Order more", "order"), ("🧾 My bill", "bill")]],
    )


async def _bill(chat_id: int, customer: dict) -> None:
    month = current_month()
    bills = await build_bills(month, customer["id"])
    lines = []
    if bills:
        b = bills[0]
        lines.append(f"🧾 <b>{month_label(month)}</b> (so far)")
        lines += [f"• {l.quantity} × {html.escape(l.item_name)} — {money(l.total)}" for l in b.lines]
        lines.append(f"<b>Total: {money(b.total)}</b>")
    else:
        lines.append(f"No pantry items recorded for {month_label(month)} yet.")
    unpaid = await db.bills.find({"customer_id": customer["id"], "status": {"$in": ["unpaid", "pending"]}}).to_list(50)
    for doc in sorted(unpaid, key=lambda d: d["month"]):
        if doc["month"] == month:
            continue
        prior = await build_bills(doc["month"], customer["id"])
        if prior:
            tag = "awaiting verification" if doc["status"] == "pending" else "unpaid"
            lines.append(f"\n⏳ {month_label(doc['month'])}: {money(prior[0].total)} ({tag})")
    await telegram.send(chat_id, "\n".join(lines), [[("💳 I've paid", "paid"), ("↩ Menu", "menu")]])


async def _ask_screenshot(chat_id: int, customer: dict, month: Optional[str]) -> None:
    if month:
        bills = await build_bills(month, customer["id"])
        due = (month, bills[0].total) if bills and bills[0].status not in ("paid", "pending") else None
    else:
        due = await amount_due(customer["id"])
    if not due:
        await telegram.send(chat_id, "You have nothing to pay right now 🎉", MENU)
        return
    settings = await get_settings()
    await _set_state(chat_id, "awaiting_screenshot", {"month": due[0]})
    await telegram.send(
        chat_id,
        f"Please send a <b>screenshot</b> of your transfer of <b>{money(due[1])}</b> for {month_label(due[0])}.\n\n"
        f"{html.escape(settings.payment_instructions)}",
        [[("✖ Cancel", "menu")]],
    )


async def _receive_screenshot(chat_id: int, customer: dict, image: bytes, ctype: str) -> None:
    state = await _state(chat_id)
    month = (state.get("data") or {}).get("month")
    if not month:
        due = await amount_due(customer["id"])
        month = due[0] if due else None
    if not month:
        await telegram.send(chat_id, "You have nothing to pay right now 🎉", MENU)
        return
    payment = await submit_payment(customer, month, image, ctype, "telegram")
    await _set_state(chat_id, "idle")
    await telegram.send(
        chat_id,
        f"📨 Thanks! Your payment of {money(payment.amount)} for {month_label(month)} is awaiting verification. "
        "You'll get a message once it's confirmed.",
        MENU,
    )


async def _extract_photo(msg: dict) -> Optional[tuple[bytes, str]]:
    if msg.get("_sim_photo"):
        return base64.b64decode(msg["_sim_photo"]), "image/png"
    if msg.get("photo"):
        return await telegram.download_file(msg["photo"][-1]["file_id"])
    doc = msg.get("document") or {}
    if str(doc.get("mime_type", "")).startswith("image/"):
        return await telegram.download_file(doc["file_id"])
    return None


async def handle_update(update: dict) -> None:
    callback = update.get("callback_query")
    msg = (callback or {}).get("message") if callback else update.get("message")
    if not msg:
        return
    chat_id: int = msg["chat"]["id"]
    sender = (callback or msg).get("from") or {}
    username = sender.get("username", "")
    data: str = (callback or {}).get("data", "")
    text: str = (msg.get("text") or "").strip() if not callback else ""
    has_media = not callback and bool(msg.get("photo") or msg.get("document") or msg.get("_sim_photo"))

    if callback:
        await telegram.answer_callback(callback.get("id", ""))
    if not update.get("_sim"):  # simulator logs its own incoming entry
        await telegram.log(chat_id, "in", text or (f"[tapped] {data}" if data else "[photo]"), has_photo=has_media)

    # Admin linking works for anyone who knows the PIN, registered or not.
    if text.startswith("/admin"):
        pin = text.removeprefix("/admin").strip()
        if pin and check_pin(pin):
            await save_settings({"admin_chat_id": chat_id})
            await telegram.send(chat_id, "🔐 This chat will now receive low-stock and payment alerts.")
        else:
            await telegram.send(chat_id, "Usage: /admin &lt;PIN&gt;")
        return

    customer = await db.customers.find_one({"telegram_chat_id": chat_id}, {"_id": 0})
    state = await _state(chat_id)

    if not customer:
        if state.get("state") == "awaiting_name" and text and not text.startswith("/"):
            customer = await get_or_create_customer(text)
            await link_telegram(customer["id"], chat_id, username)
            await _set_state(chat_id, "idle")
            await _menu(chat_id, customer["name"], "🎉 You're registered! ")
        else:
            await _set_state(chat_id, "awaiting_name")
            await telegram.send(
                chat_id,
                "👋 Welcome to the office pantry bot!\nPlease reply with your <b>name</b> "
                "(use the same name as on the old form so your tab carries over).",
            )
        return

    name = customer["name"]
    if has_media:
        photo = await _extract_photo(msg)
        if not photo:
            await telegram.send(chat_id, "Sorry, I couldn't read that image. Please try again.", MENU)
            return
        await _receive_screenshot(chat_id, customer, *photo)
        return

    cmd = text.split()[0].lower() if text.startswith("/") else ""
    if data == "menu" or cmd in ("/start", "/menu"):
        await _set_state(chat_id, "idle")
        await _menu(chat_id, name)
    elif data == "order" or cmd == "/order":
        await _items_keyboard(chat_id)
    elif data.startswith("item:"):
        await _ask_quantity(chat_id, data.split(":", 1)[1])
    elif data.startswith("qty:"):
        _, item_id, qty = data.split(":")
        await _record(chat_id, customer, item_id, int(qty))
    elif data == "bill" or cmd == "/bill":
        await _bill(chat_id, customer)
    elif data.startswith("paid") or cmd == "/paid":
        month = data.split(":", 1)[1] if data.startswith("paid:") else None
        await _ask_screenshot(chat_id, customer, month)
    elif cmd == "/help":
        await telegram.send(chat_id, HELP, MENU)
    elif state.get("state") == "awaiting_screenshot":
        await telegram.send(chat_id, "Please send the payment screenshot as a photo 📷", [[("✖ Cancel", "menu")]])
    else:
        await _menu(chat_id, name, "I didn't catch that. ")
