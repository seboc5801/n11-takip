import os
import json
import sqlite3
import asyncio
import re
import time
from telegram import Bot
from telegram.request import HTTPXRequest
import requests
from bs4 import BeautifulSoup

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

def url_to_title(url):
    try:
        part = url.split("/urun/")[1].split("?")[0]
        name_slug = part.rsplit("-", 1)[0]
        return name_slug.replace("-", " ").title()
    except:
        return "N11 Ürünü"

def check_product_page(url):
    price = 0.0
    coupons = []
    
    # Cloudscraper yerine gerçekçi tarayıcı başlıkları kullanan requests yapılandırması
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0"
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"HTTP Durum Kodu: {response.status_code} - URL: {url}")
            return {"price": 0.0, "coupons": []}
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 1. JSON-LD Kontrolü
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                if data.get('offers') and data['offers'].get('price'):
                    price = float(data['offers']['price'])
                    break
                elif data.get('@type') == 'Product' and data.get('offers'):
                    offers = data['offers']
                    if isinstance(offers, list) and offers[0].get('price'):
                        price = float(offers[0].get('price'))
                        break
                    elif isinstance(offers, dict) and offers.get('price'):
                        price = float(offers.get('price'))
                        break
            except Exception:
                pass

        # 2. HTML Seçici Kontrolü
        if price == 0.0:
            selectors = [
                '.newPrice ins', '.newPrice', '.price', '.campaignPrice', 
                '[itemprop="price"]', '.productPrice', '.unf-p-price', '.priceContainer'
            ]
            for selector in selectors:
                el = soup.select_one(selector)
                if el:
                    txt = el.get_text().strip()
                    clean = txt.replace("TL", "").replace("₺", "").replace(".", "").replace(",", ".").strip()
                    numbers = re.findall(r'\d+[\d\.,]*', clean)
                    if numbers:
                        try:
                            val = float(numbers[0].replace(".", "").replace(",", "."))
                            if val > 10:
                                price = val
                                break
                        except:
                            pass
    except Exception as e:
        print(f"Hata oluştu ({url}): {e}")
        
    return {"price": price, "coupons": coupons}

async def main():
    if BOT_TOKEN == "BURAYA_BOT_TOKEN_YAZ" or CHAT_ID == "BURAYA_CHAT_ID_YAZ":
        print("Lütfen Bot Token ve Chat ID bilgilerini girin!")
        return

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    bot = Bot(token=BOT_TOKEN, request=request)
    
    init_db()

    if not os.path.exists("products.json"):
        print("products.json dosyası bulunamadı!")
        return

    with open("products.json", "r", encoding="utf-8") as f:
        urls = json.load(f)

    print(f"Toplam {len(urls)} ürün kontrol ediliyor...")

    for url in urls:
        title = url_to_title(url)
        data = check_product_page(url)
        current_price = data["price"]
        current_coupons = data["coupons"]

        if current_price == 0.0:
            print(f"Fiyat çekilemedi: {title}")
            time.sleep(1)
            continue

        old_data = get_product_data(url)
        should_notify = False
        message_lines = [f"📢 **{title}**"]

        if old_data is None:
            should_notify = True
            message_lines.append(f"💰 Güncel Fiyat: {current_price} TL")
        else:
            if current_price < old_data["last_price"]:
                should_notify = True
                message_lines.append(f"🔥 **FİYAT DÜŞTÜ!**")
                message_lines.append(f"📉 Eski Fiyat: {old_data['last_price']} TL")
                message_lines.append(f"💰 Yeni Fiyat: {current_price} TL")

        if should_notify:
            message_lines.append(f"\n🔗 [Ürüne Git]({url})")
            full_message = "\n".join(message_lines)
            await bot.send_message(chat_id=CHAT_ID, text=full_message, parse_mode="Markdown")
            
        update_product_data(url, current_price, current_coupons)
        time.sleep(2)

if __name__ == "__main__":
    asyncio.run(main())
