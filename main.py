import os
import json
import sqlite3
import asyncio
from telegram import Bot
from curl_cffi import requests
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

def check_product_page(url):
    """N11 mobil sürümü (m.n11.com) üzerinden Cloudflare engelini aşarak fiyat çeker"""
    # Masaüstü linkini mobil linke çeviriyoruz (m.n11.com 403 engelini aşar)
    mobile_url = url.replace("www.n11.com", "m.n11.com")
    
    try:
        print(f"Mobil istek atılıyor (Cloudflare bypass): {mobile_url}")
        # Mobil tarayıcı parmak izi (safari/chrome mobile) ile istek atıyoruz
        response = requests.get(mobile_url, impersonate="safari", timeout=30)
        print(f"HTTP Durum Kodu: {response.status_code}")
        
        if response.status_code != 200:
            print(f"Hata: Mobil sayfa {response.status_code} döndürdü.")
            return {"price": 0.0, "coupons": []}
            
        soup = BeautifulSoup(response.text, 'html.parser')
        price = 0.0
        
        # 1. JSON-LD (SEO) Verisinden Fiyat Yakalama
        try:
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string)
                    if data.get('offers') and data['offers'].get('price'):
                        price = float(data['offers']['price'])
                        print(f"Mobil JSON-LD fiyatı bulundu: {price}")
                        break
                    elif data.get('@type') == 'Product' and data.get('offers'):
                        offers = data['offers']
                        if isinstance(offers, list) and offers[0].get('price'):
                            price = float(offers[0]['price'])
                            print(f"Mobil JSON-LD liste fiyatı bulundu: {price}")
                            break
                        elif isinstance(offers, dict) and offers.get('price'):
                            price = float(offers['price'])
                            print(f"Mobil JSON-LD sözlük fiyatı bulundu: {price}")
                            break
                except Exception:
                    pass
        except Exception as e:
            print(f"JSON-LD okuma hatası: {e}")

        # 2. Meta Etiketlerinden Fiyat Yakalama
        if price == 0.0:
            try:
                meta_tag = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if meta_tag and meta_tag.get('content'):
                    price = float(meta_tag['content'].replace(',', '.'))
                    print(f"Mobil Meta etiket fiyatı bulundu: {price}")
            except Exception as e:
                print(f"Meta fiyat okuma hatası: {e}")

        # 3. Mobil CSS Seçicileri ile Fiyat Yakalama
        if price == 0.0:
            selectors = ['.newPrice', '.price', '.campaignPrice', '[itemprop="price"]', '.productPrice']
            for selector in selectors:
                el = soup.select_one(selector)
                if el:
                    txt = el.get_text().strip()
                    clean = txt.replace("TL", "").replace("₺", "").replace(".", "").replace(",", ".").strip()
                    import re
                    numbers = re.findall(r'\d+[\d\.,]*', clean)
                    if numbers:
                        try:
                            val = float(numbers[0].replace(".", "").replace(",", "."))
                            if val > 10:
                                price = val
                                print(f"Mobil CSS seçici ({selector}) ile fiyat bulundu: {price}")
                                break
                        except:
                            pass

        # Kuponlar
        coupons = []
        try:
            coupon_els = soup.select('.coupon, .campaign, .seller-coupon')
            for el in coupon_els:
                c_text = el.get_text().strip()
                if c_text and c_text not in coupons:
                    coupons.append(c_text)
        except Exception:
            pass

        print(f"Okunan Sonuç -> Fiyat: {price}, Kuponlar: {coupons}")
        return {"price": price, "coupons": coupons}
        
    except Exception as e:
        print(f"Bağlantı hatası ({mobile_url}): {e}")
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
