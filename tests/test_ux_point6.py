"""Bod 6: srovnání jako vedlejší akce, čitelnější drobný text na kartě, chybějící fotka."""
import re


def _lin(c: float) -> float:
    x = c / 255
    return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4


def contrast(fg: str, bg: str) -> float:
    def L(h):
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
    a, b = L(fg), L(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def test_card_meta_contrast_stays_secondary():
    """12px #5B6B63 už má 5.6:1, ale na kartě je malé. Tmavší #3E5149 je pořád pod nadpisem."""
    old = contrast("5B6B63", "FFFFFF")
    newer = contrast("3E5149", "FFFFFF")
    on_soft = contrast("3E5149", "F6F9F7")
    title = contrast("14211B", "FFFFFF")
    assert round(old, 2) == 5.63
    assert newer >= 7 and on_soft >= 7
    assert newer < title
    css = open("lovceny/static/app.css", encoding="utf-8").read()
    assert ".pcard .p-cat{font-size:13px;line-height:1.35;color:#3E5149}" in css
    assert ".pcard .pc-shop{font-size:13px;font-weight:600;color:#3E5149}" in css


def test_compare_is_secondary_button_with_pressed_state():
    css = open("lovceny/static/app.css", encoding="utf-8").read().replace(" ", "").replace("\n", "")
    assert ".btn-cmp-quiet{align-self:center;display:inline-flex" in css
    assert "min-height:44px" in css.split(".btn-cmp-quiet{")[1].split("}")[0]
    assert ".btn-cmp-quiet.on{background:var(--brand-50,#E8F5EF)" in css
    assert ".btn-shop-sm{display:inline-flex" in css and "background:var(--brand,#0B6E4F)" in css


def test_missing_and_broken_image_markup(client_db):
    client, db, make = client_db
    missing = make([500], name="Bez fotky UX", shop="alza", category="sluchatka")
    missing.image_url = None
    broken = make([700], name="Rozbita fotka UX", shop="alza", category="sluchatka")
    broken.image_url = "https://www.alza.cz/no-such-photo-ux.jpg"
    ok = make([900], name="Allegro fotka UX", shop="allegro", category="sluchatka")
    ok.image_url = "https://a.allegroimg.com/original/11aa22/abcd/sluchatka.jpg"
    db.commit()
    html = client.get("/kategorie/sluchatka").text
    card = html.split(f'href="/produkt/{missing.id}-')[1].split('class="card pcard"')[0]
    assert 'aria-label="Fotka produktu chybí"' in card
    assert "📦" not in card
    assert "Přidat do srovnání" in card and 'aria-pressed="false"' in card
    assert "Do obchodu" in card and "btn-shop-sm" in card
    assert "onerror=" in html and "this.onerror=null" in html
    assert "data-fallback=" not in html.split(f'href="/produkt/{ok.id}-')[1].split("img-missing")[0]
    assert "/img/" not in html.split("allegroimg")[1][:500]
