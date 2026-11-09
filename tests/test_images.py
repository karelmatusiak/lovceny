"""Proxy a zmenšování obrázků obchodů (lovceny/images.py, /img/<w>)."""
import io

import httpx
import pytest
from PIL import Image

from lovceny import images

OK_URL = "https://www.mironet.cz/Images/foto.jpg"


def _jpeg(w=1200, h=800, color=(200, 30, 30)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("IMG_CACHE_DIR", str(tmp_path / "img"))
    monkeypatch.delenv("IMG_CONCURRENCY", raising=False)
    images._sem = None
    images._sem_n = None
    images.clear_failures()
    yield
    images.clear_failures()
    images._sem = None
    images._sem_n = None


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def serve(routes):
    """routes: {url: Response | callable}; robots.txt 404 (= povoleno), pokud není uvedeno."""
    calls = []

    def h(req):
        u = str(req.url)
        calls.append(u)
        if u in routes:
            r = routes[u]
            return r(req) if callable(r) else r
        if u.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(404)
    return client_for(h), calls


@pytest.mark.parametrize("url,ok", [
    (OK_URL, True),
    ("https://cdn.4home.cz/a.jpg", True),             # poddoména obchodu
    ("https://a.allegroimg.com/original/abc", True),  # CDN Allegra
    ("https://mc-static.fast.eu/pics/1/2", True),      # CDN Planeo/4Home
    ("http://www.softcom.cz/x.png", True),
    ("https://evil.example.com/a.jpg", False),
    ("https://mironet.cz.evil.com/a.jpg", False),
    ("https://evilmironet.cz/a.jpg", False),
    ("ftp://www.mironet.cz/a.jpg", False),
    ("https://user:pw@www.mironet.cz/a.jpg", False),
    ("https://www.mironet.cz:8443/a.jpg", False),
    ("file:///etc/passwd", False),
    ("", False),
])
def test_whitelist(url, ok):
    assert images.is_allowed(url) is ok


def test_srcset_attrs():
    a = images.srcset(OK_URL, 60)
    # mironet → wsrv.nl (planeo zůstává na /img/*.webp)
    assert a["src"].startswith("https://wsrv.nl/?url=") and "w=160" in a["src"] and "output=webp" in a["src"]
    assert [p.split()[-1] for p in a["srcset"].split(", ")] == ["80w", "160w", "320w"]  # až do DPR 3
    assert a["sizes"] == "60px"
    assert a["fallback"].startswith("/img/160.webp?u=https%3A%2F%2Fwww.mironet.cz")
    assert a["orig"] == OK_URL
    assert images.srcset("https://evil.example.com/a.jpg", 60) is None
    assert images.srcset(None, 60) is None
    planeo = images.srcset("https://data.planeo.cz/pics/1/2.jpg", 60)
    assert planeo["src"].startswith("/img/160.webp?u=")
    assert planeo["fallback"] == ""  # nikdy originál (multi‑MB JPEG)


ALLEGRO_ORIG = "https://a.allegroimg.com/original/11aa22/abcd/sluchatka.jpg"
ALLEGRO_S256 = "https://a.allegroimg.com/s256/11aa22/abcd/sluchatka.jpg"
ALLEGRO_S512 = "https://a.allegroimg.com/s512/11aa22/abcd/sluchatka.jpg"


def test_allegro_srcset_uses_sized_cdn_not_wsrv_or_local():
    """Karty berou s256/s512 přímo z CDN. wsrv na allegroimg 404uje, /img by stáhl /original/."""
    assert images.wsrv_blocked(ALLEGRO_ORIG)
    assert images.is_allegro_cdn(ALLEGRO_ORIG)
    assert not images.is_allegro_cdn("https://allegroimg.com.evil.com/original/x")
    assert not images.is_allegro_cdn("https://notallegroimg.com/original/x")

    card = images.srcset(ALLEGRO_ORIG, 120)  # pcard v kategorii
    assert card["src"] == ALLEGRO_S256
    assert card["srcset"] == f"{ALLEGRO_S256} 320w, {ALLEGRO_S512} 640w"
    assert card["sizes"] == "120px"
    assert card["fallback"] == ""  # onerror nesmí spadnout na /img
    assert card["orig"] == ALLEGRO_S512
    assert "wsrv.nl" not in card["src"] + card["srcset"]
    assert "/img/" not in card["src"] + card["srcset"]
    assert "/original/" not in card["src"] + card["srcset"] + card["orig"]

    # detail / malé karty (60 px): srcset končí na 320 → jen s256
    detail = images.srcset(ALLEGRO_ORIG, 60)
    assert detail["src"] == ALLEGRO_S256
    assert detail["srcset"] == f"{ALLEGRO_S256} 320w"
    assert detail["fallback"] == ""
    assert detail["orig"] == ALLEGRO_S512

    # už zmenšené i větší rendice, query string zůstane
    assert images.normalize_allegro_image(ALLEGRO_S256) == ALLEGRO_S256
    assert images.normalize_allegro_image(
        "https://a.allegroimg.com/s1024/11aa22/abcd/x.jpg?rule=1"
    ) == "https://a.allegroimg.com/s512/11aa22/abcd/x.jpg?rule=1"
    assert images.allegro_sized_url(
        "https://allegroimg.com/ORIGINAL/11aa22/abcd/x.jpg", "s128"
    ) == "https://allegroimg.com/s128/11aa22/abcd/x.jpg"
    # bez size slotu: přímá URL, pořád bez wsrv a bez /img
    bare = "https://a.allegroimg.com/files/x.jpg"
    assert images.proxy_url(bare, 160) == bare
    assert images.normalize_allegro_image(OK_URL) == OK_URL
    # Planeo/Fast se nemění
    assert images.proxy_url("https://mc-static.fast.eu/pics/1.jpg", 160).startswith("/img/160.webp?u=")
    assert images.proxy_url(OK_URL, 160).startswith("https://wsrv.nl/?url=")


def test_allegro_get_downloads_sized_rendition_not_original():
    original = "https://a.allegroimg.com/original/abc"
    sized = "https://a.allegroimg.com/s256/abc"
    c, calls = serve({sized: httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})})
    data = images.get(original, 160, client=c)
    assert Image.open(io.BytesIO(data)).width == 160
    assert sized in calls
    assert original not in calls

    images.clear_failures()
    wide = "https://a.allegroimg.com/s512/abc"
    c2, calls2 = serve({wide: httpx.Response(200, content=_jpeg(800, 600), headers={"content-type": "image/jpeg"})})
    assert images.get(original, 640, client=c2)
    assert wide in calls2
    assert original not in calls2


def test_allegro_templates_skip_wsrv_and_local_proxy(client_db):
    client, db, make = client_db
    p = make([6990], name="Sluchátka Allegro XM5", category="sluchatka")
    p.image_url = ALLEGRO_ORIG
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert ALLEGRO_S256 in html
    assert ALLEGRO_S512 in html  # og:image / JSON-LD / data-orig, ne /original/
    assert "/original/" not in html
    assert "wsrv.nl" not in html
    assert '"/img/' not in html
    assert "data-fallback=" not in html  # onerror nespouští lokální /img
    assert "onerror=" in html
    cat = client.get("/kategorie/sluchatka").text
    assert ALLEGRO_S256 in cat
    assert "/original/" not in cat
    assert "wsrv.nl" not in cat
    assert '"/img/' not in cat


def test_apply_feed_stores_allegro_s512_not_original(client_db):
    from lovceny.service import apply_feed
    from lovceny.shops.feed import FeedItem, parse_tradedoubler
    client, db, make = client_db
    url = "https://allegro.cz/nabidka/sony-wh-1000xm5-cerna"
    p = make([6990], name="Sluchátka Sony WH-1000XM5 černá", shop="allegro", category="sluchatka")
    p.image_url = ALLEGRO_ORIG
    p.offers[0].url = url
    p.offers[0].shop = "allegro"
    db.commit()
    apply_feed(db, "allegro", [FeedItem(
        item_id="1", name=p.name, url=url, price=6990, image=ALLEGRO_ORIG,
        category="Elektronika > Audio, video a domácí spotřebiče > Sluchátka > Bezdrátová",
        in_stock=True,
    )])
    db.refresh(p)
    assert p.image_url == ALLEGRO_S512

    raw = """<?xml version="1.0"?>
<products><product>
  <name>Sluchátka JBL Tune 770NC</name>
  <productImage>https://a.allegroimg.com/s1024/11aa22/abcd/jbl.jpg</productImage>
  <categories><category name="Elektronika &gt; Audio, video a domácí spotřebiče &gt; Sluchátka &gt; Bezdrátová"/></categories>
  <productUrl>https://allegro.cz/nabidka/jbl-tune-770nc</productUrl>
  <price>2490</price>
</product></products>""".encode()
    items, _total = parse_tradedoubler(raw)
    assert items[0].image == "https://a.allegroimg.com/s512/11aa22/abcd/jbl.jpg"
    apply_feed(db, "allegro", items, create_missing=True, max_create=5)
    from lovceny.models import Product
    from sqlalchemy import select
    created = db.scalar(select(Product).where(Product.name == "Sluchátka JBL Tune 770NC"))
    assert created is not None
    assert created.image_url == "https://a.allegroimg.com/s512/11aa22/abcd/jbl.jpg"


def test_resize_to_webp_keeps_ratio_and_no_upscale():
    out = Image.open(io.BytesIO(images.resize_webp(_jpeg(1200, 800), 320)))
    assert out.format == "WEBP" and out.size == (320, 213)
    small = Image.open(io.BytesIO(images.resize_webp(_jpeg(100, 50), 640)))
    assert small.size == (100, 50)


def test_resize_rgba_png_and_garbage():
    b = io.BytesIO()
    Image.new("RGBA", (400, 400), (0, 0, 0, 0)).save(b, "PNG")
    assert Image.open(io.BytesIO(images.resize_webp(b.getvalue(), 160))).mode == "RGBA"
    with pytest.raises(images.ImageError):
        images.resize_webp(b"<html>not an image</html>", 160)


def test_get_downloads_resizes_and_caches():
    c, calls = serve({OK_URL: httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})})
    data = images.get(OK_URL, 160, client=c)
    assert Image.open(io.BytesIO(data)).width == 160
    n = len(calls)
    assert images.get(OK_URL, 160, client=c) == data and len(calls) == n  # z disku, bez dalšího stažení
    assert len(list(images.cache_dir().glob("*.webp"))) == 1


def test_redirect_only_to_allowed_host():
    c, _ = serve({OK_URL: httpx.Response(302, headers={"location": "https://evil.example.com/x.jpg"})})
    with pytest.raises(images.ImageError, match="povolený"):
        images.get(OK_URL, 160, client=c)
    other = "https://www.mironet.cz/b.jpg"
    c2, _ = serve({OK_URL: httpx.Response(301, headers={"location": "/b.jpg"}),
                   other: httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})})
    images.clear_failures()
    assert images.get(OK_URL, 80, client=c2)


def test_rejects_non_image_too_big_http_error_and_robots(monkeypatch):
    c, _ = serve({OK_URL: httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})})
    with pytest.raises(images.ImageError, match="obrázek"):
        images.get(OK_URL, 160, client=c)
    images.clear_failures()
    monkeypatch.setattr(images, "MAX_BYTES", 1000)
    c, _ = serve({OK_URL: httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})})
    with pytest.raises(images.ImageError, match="velký"):
        images.get(OK_URL, 160, client=c)
    images.clear_failures()
    c, _ = serve({OK_URL: httpx.Response(403)})
    with pytest.raises(images.ImageError, match="403"):
        images.get(OK_URL, 160, client=c)
    images.clear_failures()
    c, calls = serve({"https://www.mironet.cz/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /Images/"),
                      OK_URL: httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})})
    with pytest.raises(images.ImageError, match="robots"):
        images.get(OK_URL, 160, client=c)
    assert OK_URL not in calls  # obrázek se vůbec nestáhl


def test_failure_is_remembered_briefly():
    c, calls = serve({OK_URL: httpx.Response(500)})
    with pytest.raises(images.ImageError):
        images.get(OK_URL, 160, client=c)
    n = len(calls)
    with pytest.raises(images.ImageError, match="nedávno"):
        images.get(OK_URL, 320, client=c)
    assert len(calls) == n


def test_lru_eviction(monkeypatch):
    import os
    import time
    monkeypatch.setenv("IMG_CACHE_MAX_MB", str(3000 / 1024 / 1024))  # 3000 B
    d = images.cache_dir()
    now = time.time()
    for i in range(5):
        f = d / f"{i}.webp"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (now - 100 + i, now - 100 + i))
    assert images.evict(force=True) == 3
    assert sorted(p.name for p in d.glob("*.webp")) == ["3.webp", "4.webp"]  # zůstaly nejnovější


def test_endpoint_success_headers(client_db, monkeypatch):
    client, _, _ = client_db
    monkeypatch.setattr(images, "get", lambda u, w: images.resize_webp(_jpeg(), w))
    r = client.get("/img/160", params={"u": OK_URL})
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]
    assert r.headers["etag"]
    r2 = client.get("/img/160.webp", params={"u": OK_URL})
    assert r2.status_code == 200 and r2.headers["content-type"] == "image/webp"


def test_endpoint_rejects_and_falls_back(client_db, monkeypatch):
    client, _, _ = client_db
    assert client.get("/img/160", params={"u": "https://evil.example.com/a.jpg"}).status_code == 404
    assert client.get("/img/123", params={"u": OK_URL}).status_code == 404
    assert client.get("/img/160").status_code == 404

    def boom(u, w):
        raise images.ImageError("HTTP 500")
    monkeypatch.setattr(images, "get", boom)
    r = client.get("/img/160", params={"u": OK_URL}, follow_redirects=False)
    # 200 + 1×1 WebP nespustí onerror a karta zůstane prázdná. Chyba musí být neúspěch.
    assert r.status_code == 404
    assert r.status_code != 200
    assert "no-store" in r.headers["cache-control"] and "private" in r.headers["cache-control"]
    assert r.headers.get("x-lovceny-img") == "error"
    assert r.headers.get("location") is None
    assert r.content != images.PLACEHOLDER_WEBP
    with pytest.raises(Exception):
        Image.open(io.BytesIO(r.content)).load()


def test_templates_use_srcset(client_db):
    client, db, make = client_db
    p = make([1000, 990])
    p.image_url = OK_URL
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert "wsrv.nl/?url=" in html and 'sizes="60px"' in html and 'fetchpriority="high"' in html
    assert 'data-fallback="/img/160.webp?u=' in html or 'data-fallback="/img/80.webp?u=' in html or "data-fallback=" in html
    assert "onerror=" in html and 'loading="lazy"' not in html.split("fetchpriority")[0]  # priority → no lazy on hero
    p.image_url = "https://data.planeo.cz/pics/1/x.jpg"
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert 'src="/img/' in html and ".webp?u=" in html and "wsrv.nl" not in html
    p.image_url = "https://evil.example.com/a.jpg"  # cizí host → originál, bez proxy
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert 'src="https://evil.example.com/a.jpg"' in html and "wsrv.nl/?url=https%3A%2F%2Fevil" not in html


def test_rejects_too_many_pixels(monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 100_000)
    # 500×500 = 250k > 100k
    c, _ = serve({OK_URL: httpx.Response(200, content=_jpeg(500, 500), headers={"content-type": "image/jpeg"})})
    with pytest.raises(images.ImageError, match="pixel"):
        images.get(OK_URL, 160, client=c)


def test_concurrency_serializes_misses(monkeypatch):
    """S IMG_CONCURRENCY=1 běží nejvýš jeden download+resize najednou."""
    import threading
    import time
    monkeypatch.setenv("IMG_CONCURRENCY", "1")
    images._sem = None
    images._sem_n = None
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow_jpeg(req):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1
        return httpx.Response(200, content=_jpeg(200, 150), headers={"content-type": "image/jpeg"})

    urls = [f"https://www.mironet.cz/Images/foto{i}.jpg" for i in range(4)]
    routes = {u: slow_jpeg for u in urls}
    c, _ = serve(routes)

    errs = []
    def worker(u):
        try:
            assert images.get(u, 80, client=c)
        except Exception as e:
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(u,)) for u in urls]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    assert not errs
    assert peak == 1, peak


def test_large_jpeg_draft_resizes_under_raised_cap(monkeypatch):
    """Planeo/Fast JPEG >2 MB: dřív ImageError příliš velký; teď draft → malý WebP."""
    b = io.BytesIO()
    # šum → soubor >2 MB (dřívější MAX_BYTES), stále < nový 8 MB cap
    Image.effect_noise((3200, 2400), 128).convert("RGB").save(b, "JPEG", quality=92)
    data = b.getvalue()
    assert len(data) > 2 * 1024 * 1024
    assert len(data) < images.MAX_BYTES
    out = images.resize_webp(data, 160)
    im = Image.open(io.BytesIO(out))
    assert im.format == "WEBP" and im.width == 160
    assert len(out) < 80_000


def test_planeo_template_has_no_orig_fallback(client_db):
    client, db, make = client_db
    p = make([1000, 990])
    p.image_url = "https://mc-static.fast.eu/pics/1/2.jpg"
    db.commit()
    html = client.get(f"/produkt/{p.id}").text
    assert 'src="/img/' in html and "wsrv.nl" not in html
    # data-fallback atribut chybí (prázdný fallback) – žádný originál
    assert "data-fallback=\"https://mc-static.fast.eu" not in html
    assert "data-fallback=\"https://data.planeo" not in html


# Fast JPEG z homepage karty iPhonu 15 (produkt 96). wsrv.nl ho neobslouží.
FAST_IPHONE = "https://mc-static.fast.eu/pics/30/30024375/30024375?2226686873"
WSRV_IPHONE = "https://www.mironet.cz/foto/iphone-15-black.jpg"


def _dct_scale(iw: int, ih: int, box: tuple[int, int]) -> int:
    """Stejný výběr 8/4/2/1 jako Pillow JpegImageFile.draft."""
    scale = min(iw // box[0], ih // box[1])
    chosen = 1
    for cand in (8, 4, 2, 1):
        if scale >= cand:
            chosen = cand
            break
    return chosen


def test_prefer_display_image_upgrades_fast_keeps_good_and_normalizes_allegro():
    """První Fast fotka prohraje s pozdější wsrv URL. Horší příchozí dobrou nenahradí."""
    assert images.prefer_display_image(None, FAST_IPHONE) == FAST_IPHONE
    assert images.prefer_display_image(FAST_IPHONE, WSRV_IPHONE) == WSRV_IPHONE
    assert images.prefer_display_image(WSRV_IPHONE, FAST_IPHONE) == WSRV_IPHONE
    assert images.prefer_display_image(
        WSRV_IPHONE, "https://data.planeo.cz/pics/iphone.jpg") == WSRV_IPHONE
    # jiná stejně spolehlivá fotka (další wsrv host, Allegro) uloženou nepřepisuje
    other = "https://www.alza.cz/foto/iphone-15.jpg"
    assert images.is_allowed(other) and not images.wsrv_blocked(other)
    assert images.prefer_display_image(WSRV_IPHONE, other) == WSRV_IPHONE
    assert images.prefer_display_image(WSRV_IPHONE, ALLEGRO_ORIG) == WSRV_IPHONE
    # Fast → Allegro je upgrade; /original/ se uloží jako /s512/, ne přes /img
    assert images.prefer_display_image(FAST_IPHONE, ALLEGRO_ORIG) == ALLEGRO_S512
    assert images.prefer_display_image(ALLEGRO_ORIG, FAST_IPHONE) == ALLEGRO_S512
    assert images.prefer_display_image(ALLEGRO_S256, ALLEGRO_ORIG) == ALLEGRO_S256
    huge = "https://a.allegroimg.com/s2048/11aa22/abcd/iphone.jpg"
    assert images.prefer_display_image(None, huge) == "https://a.allegroimg.com/s512/11aa22/abcd/iphone.jpg"
    assert images.prefer_display_image(huge, FAST_IPHONE) == "https://a.allegroimg.com/s512/11aa22/abcd/iphone.jpg"
    # prázdný incoming jen normalizuje; surový Fast JPEG dál nejde do prohlížeče
    assert images.prefer_display_image(FAST_IPHONE, None) == FAST_IPHONE
    assert images.prefer_display_image(FAST_IPHONE, "  ") == FAST_IPHONE
    card = images.srcset(images.prefer_display_image(FAST_IPHONE, None), 180)
    assert card is not None and card["src"].startswith("/img/")
    assert "https://mc-static.fast.eu" not in card["src"] + card["srcset"] + card["fallback"]
    good = images.srcset(images.prefer_display_image(FAST_IPHONE, WSRV_IPHONE), 180)
    assert good is not None and good["src"].startswith("https://wsrv.nl/?url=")


def test_assign_image_picks_up_later_wsrv_without_touching_identity(client_db):
    """Další feed téhož produktu vymění Fast fotku. Id, slug, název a historie ceny zůstanou."""
    from lovceny.models import PricePoint
    from lovceny.service import _assign_image, apply_feed
    from lovceny.shops.feed import FeedItem

    _, db, make = client_db
    p = make([18990, 17990], name="Apple iPhone 15 128 GB Black", category="telefony")
    p.image_url = FAST_IPHONE
    offer = p.offers[0]
    pid, slug, name, url = p.id, p.slug, p.name, offer.url
    point_ids = [pt.id for pt in offer.points]
    assert len(point_ids) >= 2
    db.commit()

    apply_feed(db, "mironet", [FeedItem(
        item_id="iphone15", name=name, url=offer.url, price=17990, image=WSRV_IPHONE,
        category="Elektronika | Mobilní telefony", in_stock=True,
    )])
    db.refresh(p)
    db.refresh(offer)
    assert p.image_url == WSRV_IPHONE
    assert p.id == pid and p.slug == slug and p.name == name and offer.url == url
    assert float(offer.price) == 17990
    kept = {pt.id for pt in db.query(PricePoint).filter_by(offer_id=offer.id)}
    assert set(point_ids) <= kept

    apply_feed(db, "mironet", [FeedItem(
        item_id="iphone15", name=name, url=offer.url, price=17990, image=FAST_IPHONE,
        category="Elektronika | Mobilní telefony", in_stock=True,
    )])
    db.refresh(p)
    db.refresh(offer)
    assert p.image_url == WSRV_IPHONE
    assert p.id == pid and p.slug == slug and p.name == name and offer.url == url
    kept = {pt.id for pt in db.query(PricePoint).filter_by(offer_id=offer.id)}
    assert set(point_ids) <= kept

    _assign_image(p, ALLEGRO_ORIG)
    assert p.image_url == WSRV_IPHONE
    fresh = make([1000], name="Jen Allegro original", category="sluchatka")
    fresh.image_url = None
    _assign_image(fresh, ALLEGRO_ORIG)
    assert fresh.image_url == ALLEGRO_S512
    _assign_image(fresh, FAST_IPHONE)
    assert fresh.image_url == ALLEGRO_S512


def test_fast_180px_card_src_is_not_640_and_stays_on_local_proxy():
    """Hero karta volá shop_img(url, 180). Na 2×/3× prohlížeč bere srcset, ne src.

    Lokální /img proto nesmí nabízet 640w. Vybraný zdroj je 320, stejně jako src.
    Prohlížeč se nesmí dostat na wsrv ani na surový multi‑MB JPEG.
    """
    from pathlib import Path

    hero = Path(__file__).resolve().parents[1].joinpath("lovceny/templates/index.html").read_text()
    assert "shop_img(h.product.image_url, 180" in hero

    assert images.wsrv_blocked(FAST_IPHONE)
    card = images.srcset(FAST_IPHONE, 180)
    assert card is not None
    assert card["src"].startswith("/img/320.webp?u=")
    assert "/img/640.webp" not in card["src"]
    assert card["sizes"] == "180px"
    assert card["fallback"] == ""
    assert [p.split()[-1] for p in card["srcset"].split(", ")] == ["80w", "160w", "320w"]
    assert "640w" not in card["srcset"]
    assert "/img/640.webp" not in card["srcset"]
    blob = card["src"] + card["srcset"] + card["fallback"]
    assert "wsrv.nl" not in blob
    assert "https://mc-static.fast.eu" not in blob
    assert "http://mc-static.fast.eu" not in blob
    # wsrv karty dál berou 2× (180 → 640) přímo z CDN, bez /img
    wsrv = images.srcset(OK_URL, 180)
    assert wsrv is not None
    assert wsrv["src"].startswith("https://wsrv.nl/?url=") and "w=640" in wsrv["src"]
    assert "640w" in wsrv["srcset"]
    assert "/img/" not in wsrv["src"]


def test_image_failure_is_not_a_silent_blank_success(client_db, monkeypatch):
    """Selhání náhledu není 200 s průhledným WebP, který karta bere jako fotku."""
    client, _, _ = client_db

    def boom(u, w):
        raise images.ImageError("timeout")

    monkeypatch.setattr(images, "get", boom)
    r = client.get("/img/640.webp", params={"u": FAST_IPHONE}, follow_redirects=False)
    assert r.status_code >= 400
    assert r.status_code != 200
    assert r.headers.get("location") is None
    assert "no-store" in r.headers["cache-control"] and "private" in r.headers["cache-control"]
    assert r.content != images.PLACEHOLDER_WEBP
    assert FAST_IPHONE not in (r.headers.get("location") or "")
    loaded = False
    if r.content:
        try:
            Image.open(io.BytesIO(r.content)).load()
            loaded = True
        except Exception:
            loaded = False
    assert not loaded


def _wait_until_shared(url: str):
    import time
    deadline = time.time() + 3
    while time.time() < deadline:
        with images._lock:
            slot = images._orig.get(url)
            if slot is not None and slot.waiters >= 2:
                return
        time.sleep(0.01)
    raise AssertionError("druhá šířka se nepřipojila k probíhajícímu stažení")


def test_parallel_widths_download_the_original_once():
    """src 320 a srcset 640 stejné Fast/Planeo URL nesmí stáhnout JPEG dvakrát."""
    import threading
    started = threading.Event()
    hold = threading.Event()

    def slow(req):
        started.set()
        if not hold.wait(timeout=3):
            return httpx.Response(500)
        return httpx.Response(200, content=_jpeg(1200, 800), headers={"content-type": "image/jpeg"})

    c, calls = serve({OK_URL: slow})
    results: dict[int, bytes] = {}
    errors: dict[int, BaseException] = {}

    def worker(w: int):
        try:
            results[w] = images.get(OK_URL, w, client=c)
        except BaseException as e:  # noqa: BLE001 — test sbírá chybu vlákna
            errors[w] = e

    threads = [threading.Thread(target=worker, args=(w,)) for w in (320, 640)]
    for th in threads:
        th.start()
    try:
        assert started.wait(timeout=3)
        _wait_until_shared(OK_URL)
    finally:
        hold.set()
    for th in threads:
        th.join(timeout=3)
    assert not errors, errors
    assert Image.open(io.BytesIO(results[320])).width == 320
    assert Image.open(io.BytesIO(results[640])).width == 640
    assert calls.count(OK_URL) == 1


def test_parallel_failed_download_is_not_retried_by_the_other_width():
    import threading
    started = threading.Event()
    hold = threading.Event()

    def slow(req):
        started.set()
        hold.wait(timeout=3)
        return httpx.Response(500)

    c, calls = serve({OK_URL: slow})
    errors: list[BaseException] = []

    def worker(w: int):
        try:
            images.get(OK_URL, w, client=c)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(w,)) for w in (320, 640)]
    for th in threads:
        th.start()
    try:
        assert started.wait(timeout=3)
        _wait_until_shared(OK_URL)
    finally:
        hold.set()
    for th in threads:
        th.join(timeout=3)
    assert len(errors) == 2
    assert all(isinstance(e, images.ImageError) for e in errors)
    assert calls.count(OK_URL) == 1
    # fail se pamatuje: další šířka už originál nestahuje
    with pytest.raises(images.ImageError, match="nedávno"):
        images.get(OK_URL, 160, client=c)
    assert calls.count(OK_URL) == 1


def test_portrait_jpeg_draft_does_not_decode_full_frame(monkeypatch):
    """iPhone Fast JPEG je 1968×2584. Box (w, w*4) u 640 px draft vypne (scale 1)."""
    box = images._jpeg_draft_size(640)
    assert _dct_scale(1968, 2584, box) >= 2
    assert _dct_scale(1968, 2584, (640, 640 * 4)) == 1
    assert _dct_scale(1968, 2584, images._jpeg_draft_size(320)) >= 2

    from PIL import JpegImagePlugin
    seen = []
    orig = JpegImagePlugin.JpegImageFile.draft

    def spy(self, mode, size):
        seen.append((mode, size))
        return orig(self, mode, size)

    monkeypatch.setattr(JpegImagePlugin.JpegImageFile, "draft", spy)
    out = images.resize_webp(_jpeg(900, 1200), 320)
    assert seen == [("RGB", (320, 1))]
    assert Image.open(io.BytesIO(out)).size == (320, 427)
