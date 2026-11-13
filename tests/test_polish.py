"""Drobné úpravy: ikony, popisek Nově, patička bez duplicit, tlačítko Do obchodu."""
import re


def test_homepage_uses_line_icons_and_places_labels(client_db):
    client, _, _ = client_db
    html = client.get("/").text
    assert "🛍️" not in html and "⚖️" not in html and "📲" not in html
    assert 'class="ico"' in html
    title = html.split('class="logo-slogan"', 1)[1].split("</p>", 1)[0]
    assert "Hlídáme ceny." in title and "Řekneme na rovinu." in title
    assert "<i>Nově</i>" not in html
    assert "Specialista na mobilní telefony" in html
    assert "Nejvíc nabízené modely" in html
    assert "Poctivý hlídač cen elektroniky, bez placených pozic" not in html
    css = open("lovceny/static/app.css", encoding="utf-8").read().replace(" ", "")
    banner = css.split(".bf-bannera{", 1)[1].split("}", 1)[0]
    assert "flex-wrap:nowrap" in banner


def test_footer_links_are_unique(client_db):
    client, _, _ = client_db
    html = client.get("/").text
    foot = html.split("<footer>", 1)[1].split("</footer>", 1)[0]
    hrefs = re.findall(r'href="([^"]+)"', foot)
    assert hrefs
    assert len(hrefs) == len(set(hrefs))
    assert 'href="/kontakt"' in foot
    assert 'href="/jak-radime"' in foot
    assert foot.count('href="/jak-radime"') == 1
    assert 'href="/o-nas#kontakt"' not in foot


def test_product_buy_shows_shop_above_do_obchodu(client_db):
    client, _, make = client_db
    p = make([4200], name="Sony WH-1000XM5", shop="alza", category="telefony")
    html = client.get(f"/produkt/{p.id}").text
    block = html.split('class="buy-block"', 1)[1].split("</div>", 1)[0]
    assert "Alza" in block.split('class="buy-shop"', 1)[1].split("</p>", 1)[0]
    button = block.split("btn-primary btn-buy", 1)[1].split("</a>", 1)[0]
    assert "Do obchodu" in button and "→" in button
    assert "Koupit za" not in button
    assert 'rel="nofollow' in block
    assert 'href="/go/' in block
