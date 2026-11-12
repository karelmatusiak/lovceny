"""Rozbitá fotka (404) nezůstane ve výběru na úvodu."""
from lovceny.images import classify_image_status, probe_images
from lovceny.main import _drop_broken_home_images


class _P:
    def __init__(self, pid, image):
        self.id = pid
        self.image_url = image


class _Pick:
    def __init__(self, pid, image):
        self.product = _P(pid, image)


def test_classify_image_status_only_hard_miss_is_broken():
    assert classify_image_status(404) is False
    assert classify_image_status(410) is False
    assert classify_image_status(200) is True
    assert classify_image_status(302) is True
    assert classify_image_status(403) is None
    assert classify_image_status(405) is None
    assert classify_image_status(500) is None


def test_probe_skips_network_for_foreign_hosts():
    assert probe_images(["https://img.example/x.jpg", ""]) == {
        "https://img.example/x.jpg": None,
    }


def test_homepage_drops_broken_photo_and_refills(monkeypatch):
    def fake(urls):
        out = {}
        for raw in urls:
            url = (raw or "").strip()
            if not url:
                continue
            out[url] = False if "broken" in url else None
        return out
    monkeypatch.setattr("lovceny.images.probe_images", fake)
    broken = _Pick(1, "https://www.bscom.cz/broken.jpg")
    spare = _Pick(2, "https://img.example/ok.jpg")
    popular_bad = _Pick(3, "https://www.bscom.cz/also-broken.jpg")
    featured, extras, popular = _drop_broken_home_images([broken], [spare], [popular_bad, spare])
    assert [h.product.id for h in featured] == [2]
    assert extras == []
    assert [h.product.id for h in popular] == [2]
