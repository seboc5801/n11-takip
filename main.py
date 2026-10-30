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
