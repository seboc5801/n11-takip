#!/usr/bin/env python3
"""
N11 fiyat ve kupon takip botu

ÖNEMLİ:
- Bu sürüm 403 / 429 gibi site engellerini aşmaya çalışmaz.
- N11 erişimi reddederse ürün kaydı güncellenmez; böylece eski fiyat silinmez.
- Kullanım için N11'in izin verdiği bir API/veri kaynağı veya erişimine izin verilen
  ürün sayfası HTML'i gerekir. Sayfa HTML'inde fiyat/kupon bilgisi yoksa sonuç bulunamaz.
- Kupon seçicileri N11'in güncel sayfa yapısına göre doğrulanmalıdır.
"""

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest


# -------------------- AYARLAR --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

DB_NAME = os.getenv("DB_NAME", "bot_data.db")
PRODUCTS_FILE = Path(os.getenv("PRODUCTS_FILE", "products.json"))

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "25"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "3"))
NOTIFY_ON_FIRST_SEEN = os.getenv("NOTIFY_ON_FIRST_SEEN", "true").lower() == "true"

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (compatible; PriceTracker/1.0; +https://example.com/bot-info)"
)

# Bunlar genel örnek seçicilerdir; N11 sayfasındaki gerçek HTML ile doğrulanmalıdır.
PRICE_SELECTORS = [
    '[itemprop="price"]',
    'meta[property="product:price:amount"]',
    '.newPrice ins',
    '.newPrice',
    '.campaignPrice',
    '.productPrice',
    '.unf-p-price',
]

COUPON_SELECTORS = [
    '[class*="coupon"]',
    '[class*="Coupon"]',
    '[data-testid*="coupon"]',
    '[data-testid*="Coupon"]',
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("n11-price-bot")


# -------------------- VERİTABANI --------------------

def db_connect():
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                url TEXT PRIMARY KEY,
                last_price REAL,
                last_coupons TEXT,
                title TEXT,
                updated_at TEXT
            )
        """)

        # Önceki sürümde oluşturulmuş tabloya eksik sütunları ekle.
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()
        }
        migrations = {
            "title": "ALTER TABLE products ADD COLUMN title TEXT",
            "updated_at": "ALTER TABLE products ADD COLUMN updated_at TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)


def decode_coupons(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return sorted({str(x).strip() for x in value if str(x).strip()})
    except (json.JSONDecodeError, TypeError):
        pass
    # Eski sürüm virgülle ayrılmış kupon metni kullanıyordu.
    return sorted({x.strip() for x in raw.split(",") if x.strip()})


def get_product_data(url: str) -> dict[str, Any] | None:
    with db_connect() as conn:
        row = conn.execute(
            "SELECT last_price, last_coupons, title, updated_at "
            "FROM products WHERE url = ?",
            (url,),
        ).fetchone()

    if not row:
        return None

    return {
        "last_price": row[0],
        "last_coupons": decode_coupons(row[1]),
        "title": row[2],
        "updated_at": row[3],
    }


def update_product_data(url: str, title: str, price: float, coupons: list[str]):
    now = datetime.now(timezone.utc).isoformat()
    coupons_json = json.dumps(sorted(set(coupons)), ensure_ascii=False)

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO products (url, last_price, last_coupons, title, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                last_price = excluded.last_price,
                last_coupons = excluded.last_coupons,
                title = excluded.title,
                updated_at = excluded.updated_at
        """, (url, price, coupons_json, title, now))


# -------------------- ÜRÜN LİSTESİ --------------------

def load_products() -> list[dict[str, str]]:
    if not PRODUCTS_FILE.exists():
        raise FileNotFoundError(f"{PRODUCTS_FILE} bulunamadı.")

    with PRODUCTS_FILE.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    if not isinstance(raw, list):
        raise ValueError("products.json bir JSON listesi olmalı.")

    products = []
    for item in raw:
        if isinstance(item, str):
            url, title = item.strip(), ""
        elif isinstance(item, dict):
            url = str(item.get("url", "")).strip()
            title = str(item.get("title", "")).strip()
        else:
            continue

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            log.warning("Geçersiz ürün adresi atlandı: %r", url)
            continue

        products.append({"url": url, "title": title})

    return products


# -------------------- SAYFA VERİSİ OKUMA --------------------

def parse_turkish_number(value: Any) -> float | None:
    """1.234,56 TL / 1234,56 / 1234.56 gibi değerleri sayıya çevirir."""
    if value is None:
        return None

    text = str(value).strip()
    text = text.replace("\u00a0", " ")
    text = re.sub(r"(?i)\b(try|tl)\b", "", text)
    text = text.replace("₺", "").replace(" ", "")
    text = re.sub(r"[^0-9,.\-]", "", text)

    if not text or not re.search(r"\d", text):
        return None

    # Türkçe biçim: 1.234,56
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    # Yalnız nokta varsa ondalık ayırıcı kabul edilir.

    try:
        number = float(text)
        if number > 0:
            return number
    except ValueError:
        return None
    return None


def walk_json(value: Any):
    """JSON-LD içindeki Product/Offer nesnelerini iç içe yapılarda bulur."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_price_from_json_ld(soup: BeautifulSoup) -> float | None:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        for node in walk_json(data):
            offers = node.get("offers")
            if offers is None and str(node.get("@type", "")).lower() == "offer":
                offers = node

            offer_nodes = offers if isinstance(offers, list) else [offers]
            for offer in offer_nodes:
                if not isinstance(offer, dict):
                    continue
                price = parse_turkish_number(
                    offer.get("price")
                    or offer.get("lowPrice")
                    or offer.get("priceSpecification", {}).get("price")
                    if isinstance(offer.get("priceSpecification", {}), dict)
                    else offer.get("price")
                )
                if price:
                    return price
    return None


def extract_price_from_html(soup: BeautifulSoup) -> float | None:
    price = extract_price_from_json_ld(soup)
    if price:
        return price

    for selector in PRICE_SELECTORS:
        for element in soup.select(selector):
            raw = element.get("content") or element.get_text(" ", strip=True)
            price = parse_turkish_number(raw)
            if price:
                return price
    return None


def extract_coupons(soup: BeautifulSoup) -> list[str]:
    found: set[str] = set()

    for selector in COUPON_SELECTORS:
        for element in soup.select(selector):
            text = element.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            if text and len(text) <= 250:
                # Boş kapsayıcıları ve genel "kupon" başlıklarını alma.
                if re.search(r"kupon|indirim|₺|TL|%|kod", text, re.IGNORECASE):
                    found.add(text)

    return sorted(found)


def extract_title(soup: BeautifulSoup, fallback: str) -> str:
    for selector in [
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
    ]:
        element = soup.select_one(selector)
        if element and element.get("content"):
            return element["content"].strip()

    heading = soup.find("h1")
    if heading:
        text = heading.get_text(" ", strip=True)
        if text:
            return text

    if soup.title and soup.title.string:
        return soup.title.string.strip()

    return fallback or "N11 Ürünü"


async def fetch_product(client: httpx.AsyncClient, product: dict[str, str]):
    url = product["url"]

    try:
        response = await client.get(url)

        if response.status_code == 403:
            return {
                "ok": False,
                "error": "HTTP 403: N11 isteğe erişim izni vermedi. Kayıt güncellenmedi.",
            }
        if response.status_code == 429:
            return {
                "ok": False,
                "error": "HTTP 429: Çok fazla istek. Daha sonra tekrar deneyin.",
            }
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        price = extract_price_from_html(soup)
        coupons = extract_coupons(soup)
        title = extract_title(soup, product.get("title", ""))

        if price is None:
            return {
                "ok": False,
                "error": "Sayfa açıldı ancak doğrulanabilir fiyat bulunamadı.",
            }

        return {
            "ok": True,
            "url": url,
            "title": title,
            "price": price,
            "coupons": coupons,
        }

    except httpx.HTTPStatusError as exc:
        return {"ok": False, "error": f"HTTP hatası: {exc.response.status_code}"}
    except httpx.RequestError as exc:
        return {"ok": False, "error": f"Bağlantı hatası: {exc}"}
    except Exception as exc:
        log.exception("Ürün okunurken beklenmeyen hata: %s", url)
        return {"ok": False, "error": f"Beklenmeyen hata: {exc}"}


# -------------------- TELEGRAM BİLDİRİMLERİ --------------------

def money(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " TL"


async def send_notification(bot: Bot, title: str, url: str, lines: list[str]):
    safe_title = html.escape(title)
    safe_url = html.escape(url, quote=True)
    message = (
        f"<b>📢 {safe_title}</b>\n\n"
        + "\n".join(html.escape(line) for line in lines)
        + f'\n\n🔗 <a href="{safe_url}">Ürüne git</a>'
    )

    await bot.send_message(
        chat_id=CHAT_ID,
        text=message,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def process_product(bot: Bot, client: httpx.AsyncClient, product: dict[str, str]):
    result = await fetch_product(client, product)

    if not result["ok"]:
        log.warning("%s | %s", product["url"], result["error"])
        return

    url = result["url"]
    title = result["title"]
    current_price = result["price"]
    current_coupons = result["coupons"]

    old = get_product_data(url)
    messages: list[str] = []

    if old is None:
        if NOTIFY_ON_FIRST_SEEN:
            messages.append(f"İlk kayıt fiyatı: {money(current_price)}")
            if current_coupons:
                messages.append("Mevcut kuponlar: " + " | ".join(current_coupons))
    else:
        old_price = old["last_price"]
        old_coupons = set(old["last_coupons"])
        new_coupons = set(current_coupons) - old_coupons
        removed_coupons = old_coupons - set(current_coupons)

        if old_price is not None and current_price < old_price:
            difference = old_price - current_price
            messages.extend([
                "🔥 FİYAT DÜŞTÜ!",
                f"Eski fiyat: {money(old_price)}",
                f"Yeni fiyat: {money(current_price)}",
                f"İndirim: {money(difference)}",
            ])

        if new_coupons:
            messages.append("🎟️ Yeni kupon/indirim bilgisi: " + " | ".join(sorted(new_coupons)))

        if removed_coupons:
            messages.append("ℹ️ Sayfada artık görünmeyen kuponlar: " + " | ".join(sorted(removed_coupons)))

    # Bildirim başarısız olursa veritabanı yine güncellenir; sonraki kontrolde
    # aynı olay tekrar denenmez. İstenirse bildirim kuyruğu ayrıca eklenebilir.
    if messages:
        try:
            await send_notification(bot, title, url, messages)
            log.info("Telegram bildirimi gönderildi: %s", title)
        except Exception:
            log.exception("Telegram bildirimi gönderilemedi: %s", title)

    # Yalnızca fiyat başarıyla okunduğunda veritabanını güncelle.
    update_product_data(url, title, current_price, current_coupons)
    log.info("Kontrol edildi: %s | %s", title, money(current_price))


# -------------------- ANA PROGRAM --------------------

async def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit(
            "BOT_TOKEN ve CHAT_ID ortam değişkenlerini ayarlayın. "
            "Token'ı kodun içine yazmayın."
        )

    try:
        products = load_products()
    except Exception as exc:
        raise SystemExit(f"Ürün listesi okunamadı: {exc}")

    if not products:
        raise SystemExit("products.json içinde geçerli ürün URL'si bulunamadı.")

    init_db()
    log.info("Toplam %s ürün kontrol edilecek.", len(products))

    request = HTTPXRequest(connect_timeout=20, read_timeout=30, write_timeout=20)
    bot = Bot(token=BOT_TOKEN, request=request)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.7",
    }

    limits = httpx.Limits(max_connections=3, max_keepalive_connections=2)
    async with httpx.AsyncClient(
        headers=headers,
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        limits=limits,
    ) as client:
        for index, product in enumerate(products, start=1):
            log.info("[%s/%s] %s", index, len(products), product["url"])
            await process_product(bot, client, product)
            if index < len(products):
                await asyncio.sleep(REQUEST_DELAY)

    await bot.shutdown()
    log.info("Kontrol tamamlandı.")


if __name__ == "__main__":
    asyncio.run(main())
