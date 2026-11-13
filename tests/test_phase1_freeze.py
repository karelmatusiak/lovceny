"""Fáze 1, první krok: captcha z Mironetu se neukazuje a vedlejší funkce jsou vypnuté.

Odhlášení starých odběrů zůstává. Obnovení je přepínač LOVCENY_SIDE_FEATURES=1.
"""
from datetime import datetime, timezone

from lovceny.models import BudgetWatch, Offer, PricePoint, PushSubscription


def test_mironet_captcha_offer_is_hidden_history_stays(client_db):
    client, db, make = client_db
    now = datetime.now(timezone.utc)
    p = make([20000], name="Pixel ověřený obchod", category="telefony", shop="alza")
    p.offers[0].url = "https://www.alza.cz/pixel-ok"
    p.offers[0].last_ok_at = now
    hidden = Offer(
        product=p, shop="mironet", url="https://www.mironet.cz/pixel-captcha-skryta",
        price=1111, in_stock=True, status="blocked", last_checked=now,
        last_error="ochrana proti botům (JS challenge)",
    )
    hidden.points.append(PricePoint(price=1111, captured_at=now))
    plain = Offer(
        product=p, shop="mironet", url="https://www.mironet.cz/pixel-http-403",
        price=2222, in_stock=True, status="blocked", last_checked=now,
        last_error="HTTP 403",
    )
    other = Offer(
        product=p, shop="softcom", url="https://www.softcom.cz/pixel-captcha",
        price=3333, in_stock=True, status="blocked", last_checked=now,
        last_error="captcha",
    )
    db.add_all([hidden, plain, other])
    db.commit()
    html = client.get(f"/produkt/{p.id}-{p.slug}").text
    assert "pixel-captcha-skryta" not in html
    assert "Koupit za 1\xa0111" not in html
    # HTTP 403 a captcha jiného obchodu zůstanou v tabulce, bez tlačítka Koupit.
    assert html.count("Obchod blokuje automatické čtení") == 2
    assert "Cenu se nepodařilo ověřit" in html
    assert "20\xa0000" in html.split('class="price">', 1)[1].split("<", 1)[0]
    assert "Do obchodu" in html.split("btn-primary btn-buy", 1)[1].split("</a>", 1)[0]
    assert "Koupit za 2\xa0222" not in html
    home = client.get("/").text
    assert "pixel-captcha-skryta" not in home
    assert "Nejvíc nabízené modely" in home
    db.expire_all()
    assert db.get(Offer, hidden.id).points[0].price == 1111


def test_only_captcha_offer_has_no_buy_button(client_db):
    client, db, make = client_db
    p = make([4444], name="Jen Mironet captcha", category="telefony", shop="mironet")
    p.offers[0].status = "blocked"
    p.offers[0].last_error = "HTTP 403 + ochrana proti botům"
    p.offers[0].url = "https://www.mironet.cz/jen-captcha"
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert "jen-captcha" not in html
    assert "btn-buy" not in html
    assert "Cena zatím není k dispozici" in html
    assert len(p.offers[0].points) == 1


def test_side_features_are_off_and_unsubscribe_still_works(client_db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPUBLIC")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "private")
    client, db, make = client_db
    p = make([1000], name="Hlídaný produkt", category="telefony")
    home = client.get("/").text
    assert 'href="/prehled"' not in home
    assert 'href="/hlidat-rozpocet"' not in home
    assert 'href="/rozsireni"' not in home
    assert "ai-hint" not in home
    assert 'id="push-box"' not in client.get(f"/produkt/{p.id}").text
    assert 'id="hlidat-rozpocet"' not in client.get("/kategorie/telefony").text
    sm = client.get("/sitemap.xml").text
    assert "/rozsireni</loc>" not in sm and "/prehled</loc>" not in sm and "/hlidat-rozpocet</loc>" not in sm
    assert "/black-friday</loc>" in sm

    assert client.get("/hlidat-rozpocet", follow_redirects=False).headers["location"] == "/"
    assert client.get("/hlidat-rozpocet", follow_redirects=False).status_code == 301
    assert client.post("/api/rozpocet", json={"category": "telefony", "max_price": 5000}).status_code == 410
    assert client.get("/prehled", follow_redirects=False).status_code == 301
    assert client.get("/prehled", follow_redirects=False).headers["location"] == "/slevy"
    assert client.get("/prehled/2026-45", follow_redirects=False).status_code == 301
    week = client.get("/prehled/2026-w45", follow_redirects=False)
    assert week.status_code == 301 and week.headers["location"] == "/slevy"
    other = client.get("/prehled/cokoliv", follow_redirects=False)
    assert other.status_code == 301 and other.headers["location"] == "/slevy"
    assert client.get("/rozsireni", follow_redirects=False).status_code == 301
    assert client.get("/rozsireni/lovceny-chrome.zip").status_code == 410
    assert client.get("/api/v1/overit", params={"url": "https://www.mironet.cz/x"}).status_code == 410
    assert client.post("/api/push/odebirat", json={
        "product_id": p.id, "target_price": 900,
        "subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "B" * 40, "auth": "a" * 16}},
    }).status_code == 404

    sub = PushSubscription(product_id=p.id, endpoint="https://fcm.googleapis.com/fcm/send/old",
                           p256dh="B" * 40, auth="a" * 16, target_price=900, token="stary-push")
    watch = BudgetWatch(token="stary-rozpocet", category_slug="telefony", max_price=5000,
                         channel="push", confirmed=True, endpoint="https://fcm.googleapis.com/fcm/send/old")
    db.add_all([sub, watch])
    db.commit()
    assert client.post("/api/push/zrusit", json={"token": "stary-push"}).json()["deleted"] == 1
    gone = client.get("/hlidat-rozpocet/zrusit/stary-rozpocet")
    assert gone.status_code == 200 and "zrušeno" in gone.text
    assert db.query(BudgetWatch).count() == 0

    import weekly_digest
    assert weekly_digest.main(["--force"]) == 0


def test_email_price_watch_is_not_on_the_product_page(client_db):
    client, db, make = client_db
    p = make([1000], name="E-mail hlídání zůstává", category="telefony")
    html = client.get(f"/produkt/{p.id}").text
    assert 'id="hlidani"' not in html and 'name="email"' not in html
    assert "Hlídání ceny" not in html and "push-box" not in html
