def fetch_store_products(store_url):
    """N11 mağaza sayfasındaki gerçek ürünleri, isimlerini ve sepet/indirimli fiyatlarını hassas bir şekilde çeker."""
    products = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en-US;q=0.7"
    }
    
    try:
        response = requests.get(store_url, headers=headers, timeout=30)
        print(f"Sayfa HTTP Kodu: {response.status_code}")
        
        if response.status_code != 200:
            print(f"Mağaza sayfasına erişilemedi: {store_url}")
            return products
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Sadece gerçek ürün kartlarını al (Genellikle li.column veya .column sınıfına sahiplerdir)
        items = soup.select('li.column, .column.loading')
        if not items:
            # Alternatif olarak genel ürün liste elemanlarını deneyelim
            items = soup.select('.catItemList li, .product-item')
            
        print(f"Bulunan gerçek ürün kartı sayısı: {len(items)}")
        
        for item in items:
            # Ürün Linki (Sadece /urun/ içeren linkleri gerçek ürün kabul edelim)
            link_el = item.select_one('a[href*="/urun/"]')
            if not link_el:
                continue
            url = link_el.get('href')
            if not url:
                continue
            if not url.startswith('http'):
                url = "https://www.n11.com" + url
            
            # Ürün Adı (Hatalı veya boş isimleri engellemek için net seçici)
            title_el = item.select_one('.productName, .title, h3, h4')
            title = title_el.get_text().strip() if title_el else ""
            if not title or len(title) < 3:
                # Eğer başlık bulunamadıysa linkin title veya text özelliğini dene
                title = link_el.get('title') or link_el.get_text().strip()
            if not title or len(title) < 3:
                title = "N11 Ürünü"
            
            # Fiyat (Önce indirimli/sepet fiyatı, yoksa normal fiyat)
            price = 0.0
            price_el = item.select_one('.discountedPrice, .newPrice, ins, .price')
            if price_el:
                txt = price_el.get_text().strip()
                # TL, ₺ ve boşlukları temizle, binlik ayraçları yok et
                clean = txt.replace("TL", "").replace("₺", "").replace(".", "").replace(",", ".").strip()
                # Sadece geçerli sayı formatını al (örn: 149.90 veya 1500)
                numbers = re.findall(r'\d+(?:\.\d+)?', clean)
                if numbers:
                    try:
                        val = float(numbers[0])
                        # 10 TL gibi saçma/yanlış çekilen düşük fiyatları elemek için taban fiyat sınırı koyalım (örn: 20 TL altı ürün olamaz)
                        if val > 20:
                            price = val
                    except:
                        pass
            
            if price > 0 and url not in [p["url"] for p in products]:
                products.append({
                    "url": url,
                    "title": title,
                    "price": price,
                    "coupons": []
                })
                
    except Exception as e:
        print(f"Mağaza taranırken hata oluştu {store_url}: {e}")
        
    print(f"Listeye eklenen geçerli ürün sayısı: {len(products)}")
    return products
