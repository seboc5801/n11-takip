import os
import json
import sqlite3
import asyncio
import re
from urllib.parse import urlsplit, unquote
from telegram import Bot
from telegram.request import HTTPXRequest
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ.get("BOT_TOKEN", "BURAYA_BOT_TOKEN_YAZ")
CHAT_ID = os.environ.get("CHAT_ID", "BURAYA_CHAT_ID_YAZ")

DB_NAME = "bot_data.db"

# Çalışan mağaza kodundaki gibi oturum ve header yapısı eklendi
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.n11.com/"
})

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
        path = unquote(urlsplit(url).path)
        slug = path.rstrip("/").split("/")[-1]
        slug = re.sub(r"-\d+$", "", slug)
        title = re.sub(r"-+", " ", slug).strip().title()
        return title if title else "N11 Ürünü"
    except:
        return "N11 Ürünü"

def check_product_page(url):
    price = 0.0
    coupons = []
    
    # Hem normal siteyi hem mobil siteyi session ile deneyelim
    urls_to_try = [url, url.replace("www.n11.com", "m.n11.com")]
    
    for target_url in urls_to_try:
        try:
            response = SESSION.get(target_url, timeout=30)
            if response.status_code != 200:
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 1. JSON-LD tarama yapısı
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    script_text = script.string or script.get_text()
                    if not script_text:
                        continue
                    data = json.loads(script_text)
                    stack = data if isinstance(data, list) else [data]
                    while stack:
                        obj = stack.pop()
                        if isinstance(obj, list):
                            stack.extend(obj)
                            continue
                        if not isinstance(obj, dict):
                            continue
                        if isinstance(obj.get("@graph"), list):
                            stack.extend(obj["@graph"])

                        offers = obj.get('offers')
                        if offers:
                            if isinstance(offers, list) and len(offers) > 0:
                                first_offer = offers[0]
                                if first_offer.get('price'):
                                    val = float(first_offer.get('price'))
                                    if val > 0:
                                        price = val
                            elif isinstance(offers, dict):
                                if offers.get('price'):
                                    val = float(offers.get('price'))
                                    if val > 0:
                                        price = val
                        if price > 0:
                            break
                except Exception:
                    pass
            
            if price > 0:
                break

            # 2. HTML Selector fallback
            selectors = ['.newPrice', '.price', '.campaignPrice', '[itemprop="price"]', '.productPrice', '.priceContainer']
            for selector in selectors:
                els = soup.select(selector)
                for el in els:
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
                if price > 0:
                    break
                    
            if price > 0:
                break
                
        except Exception as e:
            continue
        
    return {"price": price, "coupons": coupons}

async def main():
    if BOT_TOKEN == "BURAYA_BOT_TOKEN_YAZ" or CHAT_ID == "BURAYA_CHAT_ID_YAZ":
        print("Lütfen Bot Token ve Chat ID bilgilerini girin!")
        return

    request = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    bot = Bot(token=BOT_TOKEN, request=request)
    
    init_db()

    if not os.path.exists("products.json"):
        print("products.json bulunamadı!")
        return

    with open("products.json", "r", encoding="utf-8") as f:
        urls = json.load(f)

    for url in urls:
        title = url_to_title(url)
        data = check_product_page(url)
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

if __name__ == "__main__":
    asyncio.run(main())
