import os
import json
import sqlite3
import asyncio
from telegram import Bot
from playwright.async_api import async_playwright

BOT_TOKEN = os.environ.get("BOT_TOKEN", "BURAYA_BOT_TOKEN_YAZ")
CHAT_ID = os.environ.get("CHAT_ID", "BURAYA_CHAT_ID_YAZ")

DB_NAME = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            url TEXT PRIMARY KEY,
            last_price REAL,
            last_coupons TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_product_data(url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT last_price, last_coupons FROM products WHERE url = ?", (url,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "last_price": row[0], 
            "last_coupons": row[1].split(",") if row[1] else []
        }
    return None

def update_product_data(url, price, coupons):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    coupons_str = ",".join(coupons) if coupons else ""
    cursor.execute('''
        INSERT INTO products (url, last_price, last_coupons)
        VALUES (?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            last_price = excluded.last_price,
            last_coupons = excluded.last_coupons
    ''', (url, price, coupons_str))
    conn.commit()
    conn.close()

async def check_product_page(url):
    """Web sitesine async olarak gidip gerçek fiyatı ve kuponları okur"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            price = 0.0
            try:
                price_locator = page.locator(".price, .product-price, .add-to-cart-price").first
                price_text = await price_locator.inner_text()
                price = float(price_text.replace("TL", "").replace("₺", "").replace(".", "").replace(",", ".").strip())
            except Exception:
                price = 0.0

            coupons = []
            try:
                coupon_elements = await page.locator(".coupon, .seller-coupon").all()
                for elem in coupon_elements:
                    c_text = await elem.inner_text()
                    c_text = c_text.strip()
                    if c_text:
                        coupons.append(c_text)
            except Exception:
                pass

            await browser.close()
            return {"price": price, "coupons": coupons}
            
        except Exception as e:
            print(f"Hata ({url}): {e}")
            await browser.close()
            return {"price": 0.0, "coupons": []}

async def main():
    if BOT_TOKEN == "BURAYA_BOT_TOKEN_YAZ" or CHAT_ID == "BURAYA_CHAT_ID_YAZ":
        print("Lütfen Bot Token ve Chat ID bilgilerini girin!")
        return

    bot = Bot(token=BOT_TOKEN)
    init_db()

    if not os.path.exists("products.json"):
        print("products.json dosyası bulunamadı!")
        return

    with open("products.json", "r", encoding="utf-8") as f:
        products = json.load(f)

    for item in products:
        url = item["url"]
        title = item.get("title", "Ürün")
        
        data = await check_product_page(url)
        current_price = data["price"]
        current_coupons = data["coupons"]

        if current_price == 0.0:
            continue

        old_data = get_product_data(url)
        should_notify = False
        message_lines = [f"📢 **{title}**"]

        if old_data is None:
            should_notify = True
            message_lines.append(f"💰 Güncel Fiyat: {current_price} TL")
            if current_coupons:
                message_lines.append(f"🎟️ Aktif Kuponlar: {', '.join(current_coupons)}")
        else:
            if current_price < old_data["last_price"]:
                should_notify = True
                message_lines.append(f"🔥 **FİYAT DÜŞTÜ!**")
                message_lines.append(f"📉 Eski Fiyat: {old_data['last_price']} TL")
                message_lines.append(f"💰 Yeni Fiyat: {current_price} TL")

            new_coupons = [c for c in current_coupons if c not in old_data["last_coupons"]]
            if new_coupons:
                should_notify = True
                message_lines.append(f"🎟️ **YENİ KUPON VAR!**")
                message_lines.append(f"Kuponlar: {', '.join(new_coupons)}")

        if should_notify:
            message_lines.append(f"\n🔗 [Ürüne Git]({url})")
            full_message = "\n".join(message_lines)
            await bot.send_message(chat_id=CHAT_ID, text=full_message, parse_mode="Markdown")
            
        update_product_data(url, current_price, current_coupons)

if __name__ == "__main__":
    asyncio.run(main())
