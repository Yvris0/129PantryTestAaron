"""Demo data. Run: cd /app/backend && python seed.py [--reset]

Idempotent: does nothing if items already exist, unless --reset is passed.
"""

import asyncio
import random
import sys
from datetime import datetime, timedelta, timezone

from lib.db import db, ensure_indexes
from lib.pantry import month_of, tz
from models.pantry import Customer, Item, Order, Payment, Settings

ITEMS = [
    ("Nespresso Pod", "Coffee & Tea", 1.20, 40, 15, "☕", "https://www.fairprice.com.sg/search?query=nespresso"),
    ("Milo 3-in-1 Sachet", "Coffee & Tea", 0.60, 4, 10, "🥤", "https://www.fairprice.com.sg/search?query=milo%203in1"),
    ("Pokka Green Tea", "Drinks", 1.50, 18, 8, "🍵", "https://www.redmart.com/search?q=pokka%20green%20tea"),
    ("Oat Milk (1L)", "Drinks", 4.20, 2, 3, "🥛", "https://www.redmart.com/search?q=oatside"),
    ("100 Plus", "Drinks", 1.30, 24, 8, "🥫", "https://www.fairprice.com.sg/search?query=100%20plus"),
    ("Meiji Hello Panda", "Snacks", 1.80, 12, 6, "🐼", "https://shopee.sg/search?keyword=hello%20panda"),
    ("Julie's Peanut Butter Sandwich", "Snacks", 0.80, 30, 10, "🥜", "https://shopee.sg/search?keyword=julies"),
    ("Nissin Cup Noodles", "Meals", 1.90, 3, 6, "🍜", "https://www.fairprice.com.sg/search?query=nissin%20cup"),
    ("Mamee Monster", "Snacks", 0.70, 25, 10, "🍘", "https://shopee.sg/search?keyword=mamee%20monster"),
    ("Yakult (5-pack)", "Drinks", 3.10, 6, 3, "🧃", "https://www.redmart.com/search?q=yakult"),
    ("Kaya Toast Biscuits", "Snacks", 2.50, 9, 4, "🍞", "https://www.fairprice.com.sg/search?query=kaya%20biscuit"),
    ("Old Town White Coffee", "Coffee & Tea", 0.90, 22, 10, "☕", "https://shopee.sg/search?keyword=oldtown%20white%20coffee"),
]
STAFF = ["Wei Ming", "Rachel Tan", "Suresh K", "Chloe Lim", "David Goh", "Nur Aisyah"]


def receipt_svg(name: str, amount: float, ref: str) -> bytes:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640" viewBox="0 0 360 640">
<rect width="360" height="640" fill="#F4F1EC"/><rect x="0" y="0" width="360" height="120" fill="#7B1FA2"/>
<text x="180" y="70" font-family="Arial" font-size="26" fill="#fff" text-anchor="middle" font-weight="bold">PayNow</text>
<circle cx="180" cy="200" r="38" fill="#15803D"/><path d="M162 200 l12 12 l24 -26" stroke="#fff" stroke-width="7" fill="none"/>
<text x="180" y="280" font-family="Arial" font-size="18" fill="#333" text-anchor="middle">Transfer Successful</text>
<text x="180" y="330" font-family="Arial" font-size="40" fill="#111" text-anchor="middle" font-weight="bold">S${amount:.2f}</text>
<text x="30" y="400" font-family="Arial" font-size="14" fill="#666">From</text><text x="330" y="400" font-family="Arial" font-size="14" fill="#111" text-anchor="end">{name}</text>
<text x="30" y="435" font-family="Arial" font-size="14" fill="#666">To</text><text x="330" y="435" font-family="Arial" font-size="14" fill="#111" text-anchor="end">Pantry Fund</text>
<text x="30" y="470" font-family="Arial" font-size="14" fill="#666">Reference</text><text x="330" y="470" font-family="Arial" font-size="14" fill="#111" text-anchor="end">{ref}</text>
<text x="30" y="505" font-family="Arial" font-size="14" fill="#666">Txn ID</text><text x="330" y="505" font-family="Arial" font-size="14" fill="#111" text-anchor="end">PN{random.randint(10**9, 10**10 - 1)}</text>
</svg>""".encode()


async def main(reset: bool) -> None:
    if reset:
        for col in ["items", "customers", "orders", "bills", "payments", "telegram_log", "tg_sessions", "settings", "cron_runs"]:
            await db[col].drop()
        await ensure_indexes()
    elif await db.items.count_documents({}):
        print("Already seeded (pass --reset to wipe and reseed)")
        return

    random.seed(42)
    items = []
    for name, cat, price, stock, low, emoji, url in ITEMS:
        it = Item(name=name, category=cat, price=price, stock=stock, low_threshold=low, emoji=emoji, shop_url=url)
        items.append(it)
        await db.items.insert_one({**it.model_dump(), "low_alert_sent": stock <= low})

    customers = []
    for name in STAFF:
        c = Customer(name=name)
        customers.append(c)
        await db.customers.insert_one({**c.model_dump(), "name_lower": name.lower()})

    now_sg = datetime.now(tz())
    first_this = now_sg.replace(day=1, hour=10, minute=0, second=0, microsecond=0)
    first_prev = (first_this - timedelta(days=1)).replace(day=1)

    def make_orders(start: datetime, days: int, per_person: int) -> list[Order]:
        out = []
        for c in customers:
            for _ in range(per_person):
                it = random.choice(items)
                qty = random.randint(1, 3)
                when = start + timedelta(days=random.randint(0, max(days - 1, 0)), hours=random.randint(0, 8))
                when_utc = when.astimezone(timezone.utc)
                out.append(Order(customer_id=c.id, customer_name=c.name, item_id=it.id, item_name=it.name,
                                 unit_price=it.price, quantity=qty, total=round(it.price * qty, 2),
                                 source=random.choice(["web", "telegram"]), month=month_of(when_utc), created_at=when_utc))
        return out

    prev_orders = make_orders(first_prev, 26, 6)
    days_so_far = max((now_sg - first_this).days, 0) + 1
    cur_orders = make_orders(first_this, days_so_far, 3)
    # keep current-month orders in the past
    cur_orders = [o for o in cur_orders if o.created_at <= datetime.now(timezone.utc)]
    await db.orders.insert_many([o.model_dump() for o in prev_orders + cur_orders])

    prev_month = first_prev.strftime("%Y-%m")
    totals = {c.id: round(sum(o.total for o in prev_orders if o.customer_id == c.id), 2) for c in customers}
    sent_at = first_this.astimezone(timezone.utc)
    statuses = ["paid", "pending", "pending", "pending", "unpaid", "unpaid"]
    for c, status in zip(customers, statuses):
        doc = {"customer_id": c.id, "month": prev_month, "status": status, "sent_at": sent_at}
        if status == "paid":
            doc["paid_at"] = sent_at + timedelta(hours=5)
        await db.bills.insert_one(doc)
        if status in ("paid", "pending"):
            p = Payment(customer_id=c.id, customer_name=c.name, month=prev_month, amount=totals[c.id], source="telegram",
                        status="approved" if status == "paid" else "pending", created_at=sent_at + timedelta(hours=2))
            await db.payments.insert_one({**p.model_dump(), "screenshot": receipt_svg(c.name, totals[c.id], f"PANTRY {c.name.upper()}"),
                                          "content_type": "image/svg+xml"})

    await db.settings.update_one({"key": "main"}, {"$set": Settings().model_dump()}, upsert=True)
    print(f"Seeded {len(items)} items, {len(customers)} staff, {len(prev_orders) + len(cur_orders)} orders; bills sent for {prev_month}")


if __name__ == "__main__":
    asyncio.run(main("--reset" in sys.argv))
