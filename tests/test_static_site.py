"""Statický generátor: stejné URL a pravidla jako živý web, bez databáze pro návštěvníka."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient

from lovceny.categories import is_accessory_text
from lovceny.main import app, tpl
from lovceny.models import Offer, PricePoint, Product
from lovceny.seo import robots_txt, sitemap_xml
from lovceny.names import canonical_slug
from lovceny.staticsite import HTML_CACHE_CONTROL, STATIC_CACHE_CONTROL, _Redirects, build, norm_offer_url, output_path, rewrite_img_proxy, rewrite_pagination, security_headers, write_redirects
from lovceny.web import public_url
from lovceny.util import slugify


def test_preview_build_does_not_migrate(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("lovceny.db.init_db", lambda: calls.append("migrate"))
    monkeypatch.setattr(
        "lovceny.staticsite.build",
        lambda *args, **kwargs: {
            "pages": 1, "files": 1, "bytes": 1, "seconds": 0, "products": 0,
            "search_index_bytes": 0, "redirect_mode": "file", "affiliate": [],
            "url_changes": ["x"],
        },
    )
    from lovceny.staticsite import main
    assert main(["--out", str(tmp_path / "dist"), "--report", str(tmp_path / "report.json")]) == 0
    assert calls == []
    assert main(["--migrate", "--out", str(tmp_path / "dist2"), "--report", str(tmp_path / "report2.json")]) == 0
    assert calls == ["migrate"]


def test_output_path_is_html_so_pages_returns_200():
    """Pages servíruje soubor.html na adrese bez lomítka. Složka/index.html by 308 na lomítko."""
    root = Path("/tmp/lovceny-paths")
    assert output_path(root, "/") == root / "index.html"
    assert output_path(root, "/sitemap.xml") == root / "sitemap.xml"
    assert output_path(root, "/produkt/12-slug") == root / "produkt" / "12-slug.html"
    assert output_path(root, "/kategorie/telefony") == root / "kategorie" / "telefony.html"
    assert output_path(root, "/kategorie/telefony/strana/2") == root / "kategorie" / "telefony" / "strana" / "2.html"
    assert output_path(root, "/kategorie/telefony/").name == "telefony.html"


def test_rewrites_keep_pagination_and_original_photos():
    html = '<a href="https://lovceny.cz/kategorie/telefony?strana=2">2</a>'
    assert rewrite_pagination(html) == '<a href="https://lovceny.cz/kategorie/telefony/strana/2">2</a>'
    proxied = '/img/320.webp?u=https%3A%2F%2Fdata.planeo.cz%2Ffoto.jpg'
    assert rewrite_img_proxy(proxied) == "https://data.planeo.cz/foto.jpg"
    assert norm_offer_url("https://www.alza.cz/iphone?utm_source=x&idp=1") == "alza.cz/iphone?idp=1"


def test_redirect_overflow_uses_a_pages_function(tmp_path):
    book = _Redirects()
    book.add("/kontakt", "/o-nas#kontakt", 302)
    book.add("/pes/*", "/bez-serveru", 301, dynamic=True)
    for i in range(4):
        book.gone[str(i)] = "/kategorie/telefony"
        book.gone[f"{i}-stary"] = "/kategorie/telefony"
    mode = write_redirects(tmp_path, book, static_limit=3)
    assert mode == "function"
    text = (tmp_path / "_redirects").read_text()
    assert "/kontakt /o-nas#kontakt 302" in text
    assert "/produkt/0 " not in text
    script = (tmp_path / "functions" / "produkt" / "[[path]].js").read_text()
    assert '"0":"/kategorie/telefony"' in script or '"0": "/kategorie/telefony"' in script
    assert script.index("map.gone[key]") < script.index("fetchAsset(context, path)")
    assert "new Request(url, context.request)" not in script
    routes = json.loads((tmp_path / "_routes.json").read_text())
    assert routes["include"] == ["/produkt/*"]


def test_existing_product_file_is_not_redirected(tmp_path):
    book = _Redirects()
    (tmp_path / "produkt").mkdir()
    (tmp_path / "produkt" / "7-zivy.html").write_text("<html></html>", encoding="utf-8")
    book.gone["7-zivy"] = "/kategorie/telefony"
    book.gone["7"] = "/kategorie/telefony"
    book.gone["8-skryty"] = "/kategorie/telefony"
    mode = write_redirects(tmp_path, book, static_limit=20)
    assert mode == "file"
    text = (tmp_path / "_redirects").read_text()
    assert "/produkt/7-zivy " not in text
    assert "/produkt/7 /kategorie/telefony 301" in text
    assert "/produkt/8-skryty /kategorie/telefony 301" in text


def _offer(product, shop, url, price, *, stock=True, when=None, days=8):
    now = when or datetime.now(timezone.utc)
    offer = Offer(
        product=product, shop=shop, url=url, price=price, in_stock=stock,
        status="ok", last_checked=now, last_ok_at=now,
    )
    for day in range(days):
        offer.points.append(PricePoint(
            price=price, captured_at=now - timedelta(days=days - 1 - day), is_demo=False,
        ))
    return offer


def test_build_matches_live_urls_and_hides_unsafe_offers(client_db, tmp_path, monkeypatch):
    client, db, _make = client_db
    monkeypatch.setenv("ALZA_AFFILIATE_ID", "testovaci")
    now = datetime.now(timezone.utc)
    good = Product(
        name="Samsung Galaxy A55 128GB", slug=slugify("Samsung Galaxy A55 128GB"),
        category_slug="telefony", category="Mobilní telefony", ean="8806095498765",
        image_url="https://data.planeo.cz/foto/a55.jpg",
    )
    thin = Product(
        name="Samsung Galaxy A35 tenká historie", slug="samsung-galaxy-a35-tenka",
        category_slug="telefony", category="Mobilní telefony",
    )
    oos = Product(
        name="Samsung Galaxy A15 vyprodany", slug="samsung-galaxy-a15-oos",
        category_slug="telefony", category="Mobilní telefony",
    )
    stale = Product(
        name="Samsung Galaxy A25 neověřená cena", slug="samsung-galaxy-a25-neoverena",
        category_slug="telefony", category="Mobilní telefony",
    )
    accessory_name = "Samsung Galaxy A55 silikonový kryt"
    assert is_accessory_text(accessory_name)
    accessory = Product(
        name=accessory_name, slug="samsung-galaxy-a55-kryt",
        category_slug="telefony", category="Mobilní telefony",
    )
    laptop = Product(
        name="Gigabyte GAMING A16 PRO notebook 16GB", slug="gigabyte-a16-pro",
        category_slug="notebooky", category="Notebooky",
    )
    gpu = Product(
        name="Gigabyte GeForce RTX 4070", slug="gigabyte-rtx-4070",
        category_slug="notebooky", category="Notebooky",
        retired_at=now,
    )
    perfume = Product(
        name="Karl Lagerfeld parfém", slug="karl-lagerfeld-parfem",
        category_slug="telefony", category="Mobilní telefony",
        retired_at=now,
    )
    hidden = Product(
        name="Skrytý iPhone 16", slug="skryty-iphone-16",
        category_slug="telefony", category="Mobilní telefony", catalog_hidden=True,
    )
    used = Product(
        name="Apple iPhone 15 256GB Black Stav produktu: Zánovní",
        slug="apple-iphone-15-256gb-black-stav-produktu-zanovni",
        category_slug="telefony", category="Mobilní telefony",
    )
    used_blue = Product(
        name="Apple iPhone 15 256GB Blue Stav produktu: Výborný",
        slug="apple-iphone-15-256gb-blue-stav-produktu-vyborny",
        category_slug="telefony", category="Mobilní telefony", catalog_hidden=True,
    )
    no_category = Product(
        name="Skryté bez kategorie", slug="skryte-bez-kategorie", catalog_hidden=True,
    )
    review = Product(
        name="Nejistý iPhone 16e", slug="nejisty-iphone-16e",
        category_slug="telefony", category="Mobilní telefony", needs_review=True,
    )
    merged = Product(
        name="Samsung Galaxy A55 staré URL", slug="samsung-galaxy-a55-stare",
        category_slug="telefony", category="Mobilní telefony", retired_at=now,
    )
    dirty_name = '55\\" LED Samsung OM55N-S FHD 4000cd/mi SO 24/7'
    dirty = Product(
        name=dirty_name, slug=slugify(dirty_name),
        category_slug="telefony", category="Mobilní telefony",
    )
    db.add_all([
        good, thin, oos, stale, accessory, laptop, gpu, perfume, hidden, review, merged, dirty,
        used, used_blue, no_category,
    ])
    db.flush()
    merged.redirect_product_id = good.id
    db.add_all([
        _offer(good, "alza", "https://www.alza.cz/samsung-galaxy-a55", 8990),
        _offer(thin, "alza", "https://www.alza.cz/samsung-galaxy-a35", 7990, days=2),
        _offer(oos, "alza", "https://www.alza.cz/samsung-galaxy-a15", 4990, stock=False),
        _offer(stale, "alza", "https://www.alza.cz/samsung-galaxy-a25", 6990, when=now - timedelta(days=10)),
        _offer(accessory, "alza", "https://www.alza.cz/kryt-a55", 299),
        _offer(laptop, "alza", "https://www.alza.cz/gigabyte-a16", 24990),
        _offer(review, "alza", "https://www.alza.cz/iphone-16e", 19990),
        _offer(dirty, "alza", "https://www.alza.cz/samsung-om55n", 79185),
    ])
    extra = []
    for i in range(30):
        item = Product(
            name=f"Samsung Galaxy A54 varianta {i}", slug=f"samsung-galaxy-a54-{i}",
            category_slug="telefony", category="Mobilní telefony",
        )
        extra.append(item)
    db.add_all(extra)
    db.flush()
    for i, item in enumerate(extra):
        db.add(_offer(item, "mironet", f"https://www.mironet.cz/a54-{i}", 20000 + i))
    db.commit()

    out = tmp_path / "site"
    report = build(out)
    assert tpl.env.globals["STATIC_SITE"] is False
    assert report["pages"] > 40
    assert report["files"] > report["pages"]
    assert report["bytes"] > 50_000
    assert report["seconds"] > 0
    assert report["seconds"] < 120
    assert report["redirect_mode"] == "file"
    alza = next(row for row in report["affiliate"] if row["shop"] == "Alza")
    assert alza["wrapper"] == "ALZA_AFFILIATE_ID" and alza["wrapped"] >= 1 and alza["plain"] == 0
    assert report["search_index_bytes"] < 200_000
    assert report["url_changes"]
    assert not (out / "docs").exists()
    assert not (out / "preview").exists()
    assert any("Hlídání ceny" in line for line in report["not_published"])

    good_path = out / "produkt" / f"{good.id}-{good.slug}.html"
    good_html = good_path.read_text()
    assert "btn-buy" in good_html
    assert "nofollow sponsored" in good_html
    assert 'target="_blank"' in good_html
    alza_offer = next(o for o in good.offers if o.shop == "alza")
    assert f'href="/go/{alza_offer.id}"' in good_html
    assert "idp=testovaci" not in good_html
    go_map = json.loads((out / "data" / "go.json").read_text())
    assert "idp=testovaci" in go_map[str(alza_offer.id)]["u"]
    assert go_map[str(alza_offer.id)]["f"] == "https://www.alza.cz/samsung-galaxy-a55"
    go_page = (out / "go" / f"{alza_offer.id}.html").read_text()
    assert 'name="robots" content="noindex, nofollow"' in go_page
    assert "idp=testovaci" in go_page and "https://www.alza.cz/samsung-galaxy-a55" in go_page
    assert "stránku produktu" in go_page
    script = (out / "functions" / "go" / "[[path]].js").read_text()
    assert "status: 302" in script and "no-store" in script and "RESOLVE_AFFILIATE = false" in script
    routes = json.loads((out / "_routes.json").read_text())
    assert "/go/*" in routes["include"]
    assert "<svg" in good_html
    assert "https://data.planeo.cz/foto/a55.jpg" in good_html
    assert "/img/" not in good_html.replace("/static/img/", "")
    assert 'action="/produkt/' not in good_html

    thin_html = (out / "produkt" / f"{thin.id}-{thin.slug}.html").read_text()
    assert "v-none" in thin_html and "v-buy" not in thin_html
    oos_html = (out / "produkt" / f"{oos.id}-{oos.slug}.html").read_text()
    assert "btn-buy" not in oos_html and "Momentálně není skladem" in oos_html
    stale_html = (out / "produkt" / f"{stale.id}-{stale.slug}.html").read_text()
    assert "btn-buy" not in stale_html and "Cena neověřena" in stale_html

    phones = (out / "kategorie" / "telefony.html").read_text()
    assert "Galaxy A55 128GB" in phones or "Galaxy A55" in phones
    assert "silikonový kryt" not in phones
    assert "Lagerfeld" not in phones
    assert "GeForce" not in phones
    assert "?strana=" not in phones
    assert f"/kategorie/telefony/strana/2" in phones
    page2 = (out / "kategorie" / "telefony" / "strana" / "2.html").read_text()
    assert "A54" in page2
    assert "– strana 2" in page2.split("</title>", 1)[0]
    assert 'rel="canonical" href="https://lovceny.cz/kategorie/telefony/strana/2"' in page2
    assert "– strana" not in phones.split("</title>", 1)[0]
    assert not (out / "kategorie" / "notebooky.html").exists()
    assert not (out / "produkt" / f"{laptop.id}-{laptop.slug}.html").exists()
    assert not (out / "produkt" / f"{accessory.id}-{accessory.slug}.html").exists()

    index = json.loads((out / "data" / "katalog.json").read_text())
    names = {row["n"] for row in index}
    assert "Samsung Galaxy A55 128GB" in names
    assert "Karl Lagerfeld parfém" not in names
    assert "Gigabyte GeForce RTX 4070" not in names
    assert "Gigabyte GAMING A16 PRO notebook 16GB" not in names
    assert "Nejistý iPhone 16e" not in names
    assert "Skrytý iPhone 16" not in names
    from lovceny.names import card_label
    assert card_label(accessory_name) not in names
    urls = json.loads((out / "data" / "url.json").read_text())
    assert urls[norm_offer_url("https://www.alza.cz/samsung-galaxy-a55")] == f"/produkt/{good.id}-{good.slug}"

    assert not (out / "admin").exists()
    assert not (out / "produkt" / f"{hidden.id}-{hidden.slug}.html").exists()
    assert not (out / "produkt" / f"{used.id}-{used.slug}.html").exists()
    assert not (out / "produkt" / f"{used_blue.id}-{used_blue.slug}.html").exists()
    assert not (out / "produkt" / f"{perfume.id}-{perfume.slug}").exists()
    review_html = (out / "produkt" / f"{review.id}-{review.slug}.html").read_text()
    assert "noindex" in review_html and "btn-buy" not in review_html

    sitemap = (out / "sitemap.xml").read_text()
    assert sitemap == sitemap_xml(db)
    assert "/go/" not in sitemap
    assert f"/produkt/{good.id}-" in sitemap
    assert f"/produkt/{perfume.id}-" not in sitemap
    assert f"/produkt/{laptop.id}-" not in sitemap
    assert f"/produkt/{review.id}-" not in sitemap
    base = public_url()
    for loc in sitemap.split("<loc>")[1:]:
        url = loc.split("</loc>", 1)[0]
        path = urlparse(url).path or "/"
        assert output_path(out, path).is_file(), url
        if path != "/":
            assert not path.endswith("/"), url
    canon = good_html.split('rel="canonical" href="', 1)[1].split('"', 1)[0]
    og = good_html.split('property="og:url" content="', 1)[1].split('"', 1)[0]
    assert canon == og == f"{base}/produkt/{good.id}-{good.slug}"
    assert output_path(out, urlparse(canon).path).is_file()
    assert f'href="/produkt/{good.id}-{good.slug}"' in phones
    assert f'href="/produkt/{good.id}-{good.slug}/"' not in phones
    assert (out / "robots.txt").read_text() == robots_txt()
    assert (out / "hledat.html").read_text().count("katalog.js") == 1
    assert "/hledat/zive" not in (out / "hledat.html").read_text()
    assert (out / "stav.html").exists()
    assert (out / "slevy.html").exists()
    assert (out / "black-friday.html").exists()
    assert (out / "bez-serveru.html").exists()
    about_html = (out / "o-nas.html").read_text()
    assert "mailto:info@lovceny.cz" in about_html
    assert 'action="/kontakt"' not in about_html
    assert "dáme vědět e-mailem" not in about_html
    assert (out / "404.html").exists()
    assert (out / "sw.js").exists()

    redirects = (out / "_redirects").read_text()
    live = TestClient(app)
    for path in ("/kontakt", "/metodika", "/bot", "/pro-e-shopy", "/podminky-uzivani",
                 "/porovnani", "/prehled", "/rozsireni", "/hlidat-rozpocet",
                 f"/produkt/{perfume.id}-{perfume.slug}", f"/produkt/{perfume.id}",
                 f"/produkt/{laptop.id}-{laptop.slug}", f"/produkt/{laptop.id}",
                 f"/produkt/{merged.id}-{merged.slug}"):
        response = live.get(path, follow_redirects=False)
        assert response.status_code in (301, 302), path
        assert f"{path} {response.headers['location']} {response.status_code}" in redirects
    bare = live.get(f"/produkt/{good.id}")
    assert bare.status_code == 200
    assert f"/produkt/{good.id} /produkt/{good.id}-{good.slug} 301" in redirects

    assert f"/produkt/{merged.id}-{merged.slug} /produkt/{good.id}-{good.slug} 301" in redirects
    for gone in (hidden, used, used_blue):
        assert f"/produkt/{gone.id}-{gone.slug} /telefony 301" in redirects
        assert f"/produkt/{gone.id} /telefony 301" in redirects
    assert "/kategorie/notebooky /telefony 301" in redirects
    away = live.get(f"/produkt/{accessory.id}", follow_redirects=False)
    assert away.status_code == 301 and away.headers["location"] == "/telefony"
    assert f"/produkt/{no_category.id}-{no_category.slug} / 301" in redirects
    assert f"/produkt/{no_category.id} / 301" in redirects
    assert not any(
        line.startswith(f"/produkt/{good.id}-{good.slug} ") for line in redirects.splitlines()
    )
    dirty_public = canonical_slug(dirty.name, dirty.slug)
    assert dirty_public != dirty.slug
    assert f"/produkt/{dirty.id}-{dirty.slug} /produkt/{dirty.id}-{dirty_public} 301" in redirects
    assert (out / "produkt" / f"{dirty.id}-{dirty_public}.html").is_file()
    assert not (out / "produkt" / f"{dirty.id}-{dirty.slug}.html").exists()
    dirty_html = (out / "produkt" / f"{dirty.id}-{dirty_public}.html").read_text()
    dirty_h1 = dirty_html.split("<h1", 1)[1].split("</h1>", 1)[0].split(">", 1)[1]
    assert "Samsung OM55N-S" in dirty_h1 and "55" in dirty_h1
    assert "4000cd" not in dirty_h1 and "LED" not in dirty_h1
    assert "/pes/* /bez-serveru 301" in redirects
    headers = (out / "_headers").read_text()
    assert headers == security_headers()
    assert f"Cache-Control: {HTML_CACHE_CONTROL}" in headers
    assert f"Cache-Control: {STATIC_CACHE_CONTROL}" in headers
    assert headers.index("/*") < headers.index("/static/*") < headers.index("/go/*")
    assert "Strict-Transport-Security:" in headers
    assert "X-Content-Type-Options: nosniff" in headers
    assert "Referrer-Policy:" in headers
    assert "Permissions-Policy:" in headers
    assert "https://wsrv.nl" in headers and "script-src 'self' 'unsafe-inline'" in headers
    home = (out / "index.html").read_text()
    assert "SearchAction" not in home
    assert "<!--email_off-->" in home and "info@lovceny.cz" in home
    assert "www.lovceny.cz" in " ".join(report["dashboard_steps"])
    external_scripts = []
    for html_path in out.rglob("*.html"):
        text = html_path.read_text(encoding="utf-8")
        for src in re.findall(r'<script[^>]+src="([^"]+)"', text):
            if src.startswith(("http://", "https://", "//")):
                external_scripts.append(src)
    assert external_scripts == []
    assert "<script>" in home  # vložené menu musí CSP pustit přes unsafe-inline
    assert "/hlidani/* /bez-serveru 301" in redirects
