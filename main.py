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
