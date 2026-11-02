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
    assert planeo["fallback"].startswith("https://data.planeo.cz/")


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
    assert r.status_code == 302 and r.headers["location"] == OK_URL  # originál


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
