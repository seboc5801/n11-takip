import os
import json
import sqlite3
import asyncio
import re
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
    """Playwright ile Cloudflare doğrulama süresini bekleyerek mobil sayfadan fiyat çeker"""
    mobile_url = url.replace("www.n11.com", "m.n11.com")
    price = 0.0
    coupons = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-size=390,844"
            ]
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            locale="tr-TR",
            timezone_id="Europe/Istanbul"
        )
        
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        page = await context.new_page()
        try:
            print(f"Tarayıcı ile sayfaya gidiliyor: {mobile_url}")
            # Cloudflare engeline takılmamak için domcontentloaded ve bekleme süresi
            response = await page.goto(mobile_url, timeout=60000, wait_until="commit")
            
            # Cloudflare'in "Attention Required" veya bekleme ekranını atlatması için 7 saniye veriyoruz
            print("Cloudflare kontrolü için bekleniyor...")
            await asyncio.sleep(7)
            
            # Sayfada ufak bir scroll hareketi yaparak gerçek kullanıcı simülasyonu
            await page.mouse.wheel(0, 300)
            await asyncio.sleep(3)
            
            title = await page.title()
            print(f"Sayfa Başlığı: {title}")
            
            # JavaScript ile JSON-LD veya sayfa içi fiyat elementlerini yakalama
            price = await page.evaluate("""() => {
                // 1. JSON-LD kontrolü
                const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (let script of scripts) {
                    try {
                        const data = JSON.parse(script.innerText.trim());
                        if (data.offers && data.offers.price) return parseFloat(data.offers.price);
                        if (data['@type'] === 'Product' && data.offers) {
                            if (Array.isArray(data.offers)) return parseFloat(data.offers[0].price);
                            if (data.offers.price) return parseFloat(data.offers.price);
                        }
                    } catch (e) {}
                }
                
                // 2. CSS seçicileri ile arama
                const selectors = ['.newPrice', '.price', '.campaignPrice', '[itemprop="price"]', '.productPrice', 'ins'];
                for (let sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const text = el.innerText || '';
                        const clean = text.replace('TL', '').replace('₺', '').replace('.', '').replace(',', '.').trim();
                        const match = clean.match(/\\d+/);
                        if (match) {
                            const val = parseFloat(match[0]);
                            if (val > 10) return val;
                        }
                    }
                }
                return 0.0;
            """)
            
            print(f"Okunan Fiyat: {price}")

        except Exception as e:
            print(f"Tarayıcı hata oluştu: {e}")
        finally:
            await browser.close()
            
    return {"price": price, "coupons": coupons}

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
            print(f"Fiyat çekilemedi: {title}")
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
                message_lines.append(f"Kuponlar: {', '.join(new_new_coupons := new_coupons)}")

        if should_notify:
            message_lines.append(f"\n🔗 [Ürüne Git]({url})")
            full_message = "\n".join(message_lines)
            await bot.send_message(chat_id=CHAT_ID, text=full_message, parse_mode="Markdown")
            
        update_product_data(url, current_price, current_coupons)

if __name__ == "__main__":
    asyncio.run(main())
