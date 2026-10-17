import os
import json
import sqlite3
import asyncio
import re
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
            title TEXT,
            last_price REAL,
            last_coupons TEXT
        )
    ''')
    conn.commit()
    conn.close()

def get_product_data(url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT title, last_price, last_coupons FROM products WHERE url = ?", (url,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "title": row[0],
            "last_price": row[1], 
            "last_coupons": row[2].split(",") if row[2] else []
        }
    return None

def update_product_data(url, title, price, coupons):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    coupons_str = ",".join(coupons) if coupons else ""
    cursor.execute('''
        INSERT INTO products (url, title, last_price, last_coupons)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title = excluded.title,
            last_price = excluded.last_price,
            last_coupons = excluded.last_coupons
    ''', (url, title, price, coupons_str))
    conn.commit()
    conn.close()

def fetch_store_products(store_url):
    """N11 mağaza sayfasındaki tüm ürünleri, isimlerini ve sepet/indirimli fiyatlarını çeker."""
    products = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    
    try:
        response = requests.get(store_url, headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"Mağaza sayfasına erişilemedi: {store_url} (Kod: {response.status_code})")
            return products
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # N11 mağaza sayfasındaki ürün kartları
        items = soup.select('.column, .productItem, li.column')
        
        for item in items:
            # Ürün Linki
            link_el = item.select_one('a')
            if not link_el:
                continue
            url = link_el.get('href')
            if not url:
                continue
            if not url.startswith('http'):
                url = "https://www.n11.com" + url
            
            # Ürün Adı
            title_el = item.select_one('.productName, .title, h3, h4')
            title = title_el.get_text().strip() if title_el else "N11 Ürünü"
            
            # Fiyat (Önce sepet/indirimli fiyat aranır, yoksa normal fiyat alınır)
            price = 0.0
            price_el = item.select_one('.discountedPrice, .newPrice, .price, .value')
            if price_el:
                txt = price_el.get_text().strip()
                clean = txt.replace("TL", "").replace("₺", "").replace(".", "").replace(",", ".").strip()
                numbers = re.findall(r'\d+[\d\.,]*', clean)
                if numbers:
                    try:
                        val = float(numbers[0].replace(".", "").replace(",", "."))
                        if val > 0:
                            price = val
                    except:
                        pass
            
            if price > 0:
                products.append({
                    "url": url,
                    "title": title,
                    "price": price,
                    "coupons": []
                })
                
    except Exception as e:
        print(f"Mağaza taranırken hata oluştu {store_url}: {e}")
        
    return products

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
        store_urls = json.load(f)

    # products.json içinde artık mağaza linkleri yer alıyor (Örn: https://www.n11.com/magaza/komili)
    for store_url in store_urls:
        print(f"Mağaza taranıyor: {store_url}")
        store_products = fetch_store_products(store_url)
        
        for prod in store_products:
            url = prod["url"]
            title = prod["title"]
            current_price = prod["price"]
            current_coupons = prod["coupons"]

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
                
            update_product_data(url, title, current_price, current_coupons)

if __name__ == "__main__":
    asyncio.run(main())
