import json
import re
import time
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

import requests
from bs4 import BeautifulSoup


# ------------------------------------------------------------
# AYARLAR
# ------------------------------------------------------------

PRODUCTS_FILE = "products.json"
MIN_FIYAT = 10.0
MAX_FIYAT = 1_000_000.0
SAYFA_SAYISI = 3
BEKLEME_SANIYESI = 1

BASE_URL = "https://www.n11.com/"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": BASE_URL,
})

MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.6 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}


# ------------------------------------------------------------
# YARDIMCI FONKSİYONLAR
# ------------------------------------------------------------

def urun_url_temizle(url):
    """Ürün URL'sini mutlak hale getirir; sorgu parametrelerini korur."""
    url = urljoin(BASE_URL, str(url).strip())
    parsed = urlsplit(url)
    return urlunsplit((
        parsed.scheme or "https",
        parsed.netloc or "www.n11.com",
        parsed.path.rstrip("/"),
        parsed.query,
        ""
    ))


def url_to_title(url):
    try:
        path = unquote(urlsplit(url).path)
        slug = path.rstrip("/").split("/")[-1]
        slug = re.sub(r"-\d+$", "", slug)
        title = re.sub(r"-+", " ", slug).strip()
        return title.title() if title else "N11 Ürünü"
    except Exception:
        return "N11 Ürünü"


def fiyat_cevir(value):
    """30.000,90 / 30.000 / 30000.90 gibi fiyatları sayıya çevirir."""
    if value is None:
        return 0.0

    text = str(value).replace("\xa0", " ").strip()
    text = text.replace("₺", "").replace("TL", "").replace("tl", "")
    text = re.sub(r"[^\d,.\-]", "", text)

    if not text or not re.search(r"\d", text):
        return 0.0

    # Türkçe ondalık ayırıcı virgül ise noktaları binlik kabul et.
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif text.count(".") > 1:
        text = text.replace(".", "")
    elif "." in text:
        # Tek noktanın ardından üç hane varsa binlik ayırıcıdır.
        left, right = text.rsplit(".", 1)
        if len(right) == 3 and left.isdigit() and right.isdigit():
            text = left + right

    try:
        result = float(text)
        if MIN_FIYAT <= result <= MAX_FIYAT:
            return result
    except (TypeError, ValueError):
        pass

    return 0.0


def jsonld_fiyatlarini_bul(soup):
    """Ürün sayfasındaki JSON-LD Product/Offer fiyatlarını arar."""
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        stack = data if isinstance(data, list) else [data]

        while stack:
            obj = stack.pop()

            if isinstance(obj, list):
                stack.extend(obj)
                continue

            if not isinstance(obj, dict):
                continue

            graph = obj.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)

            offers = obj.get("offers")
            if isinstance(offers, dict):
                offers = [offers]

            if isinstance(offers, list):
                for offer in offers:
                    if not isinstance(offer, dict):
                        continue

                    candidates = [
                        offer.get("price"),
                        offer.get("lowPrice"),
                    ]

                    for candidate in candidates:
                        price = fiyat_cevir(candidate)
                        if price:
                            return price

    return 0.0


def html_fiyatlarini_bul(soup):
    """Bilinen fiyat alanlarını ve meta etiketlerini kontrol eder."""
    selectors = [
        '[itemprop="price"]',
        'meta[property="product:price:amount"]',
        'meta[property="og:price:amount"]',
        'meta[itemprop="price"]',
        '[data-testid*="price"]',
        '[class*="newPrice"]',
        '[class*="priceContainer"]',
        '[class*="productPrice"]',
        '[class*="campaignPrice"]',
        '[class*="price-val"]',
        '[class*="proDetailPrice"]',
        '.newPrice ins',
        '.newPrice',
        '.price',
    ]

    for selector in selectors:
        for element in soup.select(selector):
            value = (
                element.get("content")
                or element.get("data-price")
                or element.get_text(" ", strip=True)
            )
            price = fiyat_cevir(value)
            if price:
                return price

    return 0.0


def metinden_fiyat_bul(soup):
    """Son çare olarak sayfa metninde TL/₺ ile yazılmış fiyatı arar."""
    page_text = soup.get_text(" ", strip=True)

    pattern = (
        r'(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|\d+(?:,\d{1,2})?)'
        r'\s*(?:TL|₺)'
    )

    for match in re.findall(pattern, page_text, flags=re.IGNORECASE):
        price = fiyat_cevir(match)
        if price:
            return price

    return 0.0


# ------------------------------------------------------------
# ÜRÜN BULMA VE FİYAT ÇEKME
# ------------------------------------------------------------

def kartlari_bul(soup, page_url):
    links = soup.select('a[href*="/urun/"]')
    results = []
    seen = set()

    for link in links:
        href = link.get("href")
        if not href:
            continue

        product_url = urun_url_temizle(urljoin(page_url, href))
        if "/urun/" not in urlsplit(product_url).path:
            continue

        # Aynı ürünün farklı mağaza/renk parametreleri yerine URL'yi koruyup
        # tam URL üzerinden tekilleştir.
        if product_url in seen:
            continue

        seen.add(product_url)
        results.append(product_url)

    return results


def check_product_page(url):
    """Ürün detay sayfasından fiyatı almaya çalışır."""
    urls_to_test = [
        url.replace("www.n11.com", "m.n11.com"),
        url,
    ]

    for target_url in dict.fromkeys(urls_to_test):
        try:
            headers = (
                MOBILE_HEADERS
                if "m.n11.com" in target_url
                else SESSION.headers
            )

            response = SESSION.get(
                target_url,
                headers=headers,
                timeout=25,
                allow_redirects=True,
            )

            if response.status_code != 200:
                print(
                    f"  Sayfa açılamadı: HTTP {response.status_code} "
                    f"({target_url})"
                )
                continue

            soup = BeautifulSoup(response.text, "html.parser")

            # 1. En güvenilir kaynak: yapılandırılmış ürün verisi.
            price = jsonld_fiyatlarini_bul(soup)

            # 2. HTML fiyat alanları.
            if not price:
                price = html_fiyatlarini_bul(soup)

            # 3. Sayfa metninde TL/₺ araması.
            if not price:
                price = metinden_fiyat_bul(soup)

            if price:
                return {
                    "price": round(price, 2),
                    "title": url_to_title(url),
                    "url": url,
                }

            # Sayfanın bot korumasına takılıp takılmadığını anlamak için
            # kısa bir teşhis mesajı.
            page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if any(word in page_title.lower() for word in [
                "robot", "captcha", "erişim", "access denied"
            ]):
                print(f"  N11 koruma sayfası döndürdü: {page_title}")

        except requests.RequestException as error:
            print(f"  Bağlantı hatası: {error}")
        except Exception as error:
            print(f"  Sayfa okunamadı: {error}")

    return None


def urun_listesini_yukle():
    try:
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, list):
            raise ValueError("products.json bir JSON listesi olmalı.")

        return data

    except FileNotFoundError:
        print(f"HATA: {PRODUCTS_FILE} dosyası bulunamadı.")
    except (json.JSONDecodeError, ValueError) as error:
        print(f"HATA: products.json okunamadı: {error}")

    return []


def urunleri_topla(target_list):
    product_urls = set()

    for item in target_list:
        if not isinstance(item, str):
            print(f"Geçersiz ürün girdisi atlandı: {item}")
            continue

        item = item.strip()
        if not item:
            continue

        if "arama?q=" in item or "kategori" in item:
            print(f"\nArama sayfası taranıyor: {item}")

            for page in range(1, SAYFA_SAYISI + 1):
                page_url = (
                    f"{item}&pg={page}"
                    if "?" in item
                    else f"{item}?pg={page}"
                )

                try:
                    response = SESSION.get(page_url, timeout=25)
                    if response.status_code != 200:
                        print(
                            f"  Arama sayfası HTTP {response.status_code}: "
                            f"{page_url}"
                        )
                        break

                    soup = BeautifulSoup(response.text, "html.parser")
                    found_links = kartlari_bul(soup, page_url)

                    if not found_links:
                        print(f"  Sayfa {page}: ürün bağlantısı bulunamadı.")
                        break

                    before = len(product_urls)
                    product_urls.update(found_links)
                    added = len(product_urls) - before

                    print(
                        f"  Sayfa {page}: {len(found_links)} bağlantı bulundu, "
                        f"{added} yeni ürün eklendi."
                    )

                    time.sleep(BEKLEME_SANIYESI)

                except requests.RequestException as error:
                    print(f"  Arama sayfası hatası: {error}")
                    break

        else:
            product_urls.add(urun_url_temizle(item))

    return sorted(product_urls)


# ------------------------------------------------------------
# PROGRAM
# ------------------------------------------------------------

def main():
    print("N11 ürün taraması başladı.\n")

    target_list = urun_listesini_yukle()
    if not target_list:
        print("Taranacak ürün veya arama adresi bulunamadı.")
        return

    product_urls = urunleri_topla(target_list)

    print(f"\nToplam {len(product_urls)} benzersiz ürün bulundu.")
    print("Ürün fiyatları kontrol ediliyor...\n")

    bulunan = []
    bulunamayan = 0

    for index, url in enumerate(product_urls, start=1):
        title = url_to_title(url)
        print(f"[{index}/{len(product_urls)}] Kontrol: {title}")

        result = check_product_page(url)

        if result:
            bulunan.append(result)
            print(
                f"  ÜRÜN BULUNDU: {result['title']} | "
                f"{result['price']:.2f} TL"
            )
        else:
            bulunamayan += 1
            print(f"  FİYAT ÇEKİLEMEDİ: {url}")

        time.sleep(BEKLEME_SANIYESI)

    print("\n" + "=" * 60)
    print("TARAMA SONUCU")
    print("=" * 60)
    print(f"Bulunan ürün: {len(bulunan)}")
    print(f"Fiyatı alınamayan: {bulunamayan}")

    if bulunan:
        print("\nBULUNAN ÜRÜNLER:\n")
        for item in bulunan:
            print(f"{item['title']} | {item['price']:.2f} TL")
            print(item["url"])
            print("-" * 60)

        # Sonuçları ayrıca JSON dosyasına kaydet.
        with open("bulunan_urunler.json", "w", encoding="utf-8") as file:
            json.dump(bulunan, file, ensure_ascii=False, indent=2)

        print("\nÜrünler bulunan_urunler.json dosyasına kaydedildi.")


if __name__ == "__main__":
    main()
