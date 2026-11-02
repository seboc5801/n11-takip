import json
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

import requests
from bs4 import BeautifulSoup

# Bu sürüm ürün detay sayfalarına GİRMEZ.
# Fiyatı doğrudan N11 arama sonuçlarındaki ürün kartlarından almaya çalışır.

PRODUCTS_FILE = "products.json"
OUTPUT_FILE = "bulunan_urunler.json"
DEBUG_FILE = "n11_kart_teshisi.txt"
BASE_URL = "https://www.n11.com/"
SAYFA_SAYISI = 3
BEKLEME_SANIYESI = 1
MIN_FIYAT = 10
MAX_FIYAT = 1_000_000

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_URL,
})


def fiyat_cevir(value):
    if value is None:
        return 0.0
    s = str(value).replace("\xa0", " ").strip()
    s = s.replace("₺", "").replace("TL", "").replace("tl", "")
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or not re.search(r"\d", s):
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif "." in s:
        left, right = s.rsplit(".", 1)
        if len(right) == 3 and left.isdigit() and right.isdigit():
            s = left + right
    try:
        n = float(s)
        return n if MIN_FIYAT <= n <= MAX_FIYAT else 0.0
    except ValueError:
        return 0.0


def temiz_url(url, base=BASE_URL):
    p = urlsplit(urljoin(base, str(url).strip()))
    return urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def urun_adi(url, anchor=None):
    if anchor:
        for attr in ("title", "aria-label"):
            if anchor.get(attr) and len(anchor[attr].strip()) > 5:
                return anchor[attr].strip()
        txt = " ".join(anchor.get_text(" ", strip=True).split())
        if len(txt) > 5:
            return txt[:250]
    slug = unquote(urlsplit(url).path).rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return re.sub(r"-+", " ", slug).strip().title() or "N11 Ürünü"


def karttan_fiyat_bul(anchor):
    # Ürün bağlantısının kendisi ve yakın kapsayıcıları içinde fiyat ara.
    selectors = [
        '[itemprop="price"]',
        'meta[property="product:price:amount"]',
        'meta[itemprop="price"]',
        '[data-testid*="price"]',
        '[class*="price"]',
        '[class*="Price"]',
        '[class*="amount"]',
        '[class*="Amount"]',
    ]

    current = anchor
    best_text = ""
    for _ in range(7):
        if current is None:
            break

        for selector in selectors:
            try:
                elements = current.select(selector)
            except Exception:
                elements = []
            for el in elements[:12]:
                raw = (
                    el.get("content")
                    or el.get("data-price")
                    or el.get("value")
                    or el.get_text(" ", strip=True)
                )
                price = fiyat_cevir(raw)
                if price:
                    return price, str(el)[:1200]

        text = " ".join(current.get_text(" ", strip=True).split())
        if text:
            best_text = text[:1800]
            # Kart metninde TL/₺ yanında yazan fiyatı yakala.
            pattern = r'(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)\s*(?:TL|₺)'
            for match in re.findall(pattern, text, flags=re.I):
                price = fiyat_cevir(match)
                if price:
                    return price, text[:1200]

        current = current.parent

    return 0.0, best_text


def arama_kartlarini_oku(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select('a[href*="/urun/"]')
    products = {}
    debug = []

    for a in anchors:
        href = a.get("href")
        if not href:
            continue
        url = temiz_url(href, page_url)
        if "/urun/" not in urlsplit(url).path:
            continue

        price, evidence = karttan_fiyat_bul(a)
        name = urun_adi(url, a)
        old = products.get(url)

        if old is None or (not old["fiyat"] and price):
            products[url] = {
                "urun_adi": name,
                "fiyat": round(price, 2) if price else None,
                "url": url,
            }

        if not price and len(debug) < 8:
            debug.append(
                "\nÜRÜN: " + name +
                "\nURL: " + url +
                "\nKART METNİ/İZİ: " + (evidence or "(kart metni bulunamadı)")[:1200] +
                "\nHTML: " + str(a.parent)[:1800] +
                "\n" + "-" * 70
            )

    return list(products.values()), debug, len(anchors)


def urun_listesini_yukle():
    try:
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("products.json bir liste olmalı.")
        return data
    except Exception as e:
        print(f"products.json okunamadı: {e}")
        return []


def main():
    targets = urun_listesini_yukle()
    if not targets:
        print("Taranacak kayıt yok.")
        return

    all_products = {}
    debug_parts = []
    search_count = 0

    print("N11 arama sonuçlarından fiyat çekme başladı.")
    print("ÖNEMLİ: Ürün detay sayfalarına istek gönderilmeyecek.\n")

    for item in targets:
        if not isinstance(item, str) or not item.strip():
            continue
        item = item.strip()

        if "/urun/" in item:
            url = temiz_url(item)
            all_products[url] = {
                "urun_adi": urun_adi(url),
                "fiyat": None,
                "url": url,
            }
            print(f"Doğrudan ürün adresi kaydedildi (fiyat arama kartında bulunamaz): {url}")
            continue

        if "arama?q=" not in item and "kategori" not in item:
            print(f"Atlandı, arama adresi değil: {item}")
            continue

        for page in range(1, SAYFA_SAYISI + 1):
            page_url = f"{item}&pg={page}" if "?" in item else f"{item}?pg={page}"
            print(f"\nArama sayfası: {page_url}")
            try:
                response = session.get(page_url, timeout=30)
                print(f"HTTP {response.status_code} | HTML: {len(response.text)} karakter")
                if response.status_code != 200:
                    continue

                products, debug, anchor_count = arama_kartlarini_oku(response.text, page_url)
                search_count += 1
                priced = 0
                for product in products:
                    url = product["url"]
                    previous = all_products.get(url)
                    if previous is None or (previous.get("fiyat") is None and product["fiyat"]):
                        all_products[url] = product
                    if product["fiyat"]:
                        priced += 1

                print(f"Ürün bağlantısı: {anchor_count} | Kart: {len(products)} | Fiyatı bulunan: {priced}")
                debug_parts.extend(debug)
                time.sleep(BEKLEME_SANIYESI)

            except requests.RequestException as e:
                print(f"Arama isteği başarısız: {e}")

    results = list(all_products.values())
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    with open(DEBUG_FILE, "w", encoding="utf-8") as f:
        f.write(
            "N11 arama kartı teşhis çıktısı\n"
            "Bu dosya, fiyatı bulunamayan ilk ürün kartlarının HTML örneklerini içerir.\n"
            + "\n".join(debug_parts)
        )

    priced = [p for p in results if p.get("fiyat")]
    print("\n" + "=" * 55)
    print(f"Arama sayfası sayısı: {search_count}")
    print(f"Toplam benzersiz ürün: {len(results)}")
    print(f"Fiyatı bulunan ürün: {len(priced)}")
    print(f"Fiyatı bulunamayan ürün: {len(results) - len(priced)}")
    print(f"Sonuç dosyası: {OUTPUT_FILE}")
    print(f"Teşhis dosyası: {DEBUG_FILE}")

    if priced:
        print("\nİLK BULUNAN FİYATLAR:")
        for p in priced[:30]:
            print(f"{p['urun_adi']} | {p['fiyat']:.2f} TL | {p['url']}")


if __name__ == "__main__":
    main()
