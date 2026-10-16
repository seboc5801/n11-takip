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
    """N11 mobil sürümü (m.n11.com) üzerinden Cloudflare engelini aşarak fiyat okur"""
    
    # Normal n11 linkini mobil linke dönüştürme (Örn: www.n11.com -> m.n11.com)
    mobile_url = url.replace("www.n11.com", "m.n11.com")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        
        # Mobil cihaz (iPhone/Android) görünümü ve User-Agent simülasyonu
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
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
            print(f"Mobil sayfaya gidiliyor: {mobile_url}")
            response = await page.goto(mobile_url, timeout=60000, wait_until="domcontentloaded")
            print(f"HTTP Durum Kodu: {response.status if response else 'Bilinmiyor'}")
            
            title = await page.title()
            print(f"Sayfa Başlığı: {title}")
            
            await asyncio.sleep(4)
            
            price = 0.0
            
            # 1. Mobil SEO (JSON-LD) Verisi
            try:
                price = await page.evaluate("""() => {
                    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (let script of scripts) {
                        try {
                            const text = script.innerText.trim();
                            if (!text) continue;
                            const data = JSON.parse(text);
                            if (data.offers && data.offers.price) {
                                return parseFloat(data.offers.price);
                            }
                            if (data['@type'] === 'Product' && data.offers) {
                                if (Array.isArray(data.offers)) {
                                    return parseFloat(data.offers[0].price);
                                } else if (data.offers.price) {
                                    return parseFloat(data.offers.price);
                                }
                            }
                        } catch (e) {}
                    }
                    return 0.0;
                """)
                if price > 0:
                    print(f"Mobil JSON-LD üzerinden fiyat bulundu: {price}")
            except Exception as e:
                print(f"JSON-LD okuma hatası: {e}")

            # 2. Mobil CSS Seçicileri
            if price == 0.0:
                price_selectors = [
                    ".newPrice", 
                    ".price-container", 
                    ".product-price", 
                    ".price",
                    "[itemprop='price']",
                    ".campaignPrice"
                ]
                for selector in price_selectors:
                    el = page.locator(selector).first
                    if await el.count() > 0:
                        txt = await el.inner_text()
                        clean = txt.replace("TL", "").replace("₺", "").replace(".", "").replace(",", ".").strip()
                        try:
                            # Metin içindeki ilk geçerli sayısal fiyatı yakalama
                            import re
                            numbers = re.findall(r'\d+[\d\.,]*', clean)
                            if numbers:
                                price_str = numbers[0].replace(".", "").replace(",", ".")
                                price = float(price_str)
                                print(f"Mobil CSS seçici ({selector}) ile fiyat bulundu: {price}")
                                break
                        except:
                            pass

            # Kupon ve kampanya etiketleri
            coupons = []
            try:
                coupon_elements = await page.locator(".coupon, .campaign, .seller-coupon").all()
                for elem in coupon_elements:
                    c_text = await elem.inner_text()
                    c_text = c_text.strip()
                    if c_text and c_text not in coupons:
                        coupons.append(c_text)
            except Exception:
                pass

            await browser.close()
            print(f"Okunan Sonuç -> Fiyat: {price}, Kuponlar: {coupons}")
            return {"price": price, "coupons": coupons}
            
        except Exception as e:
            print(f"Hata oluştu ({mobile_url}): {e}")
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
                message_lines.append(f"Kuponlar: {', '.join(new_coupons)}")

        if should_notify:
            message_lines.append(f"\n🔗 [Ürüne Git]({url})")
            full_message = "\n".join(message_lines)
            await bot.send_message(chat_id=CHAT_ID, text=full_message, parse_mode="Markdown")
            
        update_product_data(url, current_price, current_coupons)

if __name__ == "__main__":
    asyncio.run(main())
