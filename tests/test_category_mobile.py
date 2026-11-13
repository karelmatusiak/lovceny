"""Kategorie na mobilu: dvě karty, jeden stav u ceny, ikona srovnání, řada odkazů."""
from lovceny.models import Offer


def test_category_mobile_css():
    css = open("lovceny/static/app.css", encoding="utf-8").read().replace(" ", "")
    block = css.split("@media(max-width:720px){")[1].split("}")[0]
    assert "repeat(2,minmax(0,1fr))" in block
    assert ".cat-hero.landing-links{flex-wrap:nowrap;overflow-x:auto" in css
    assert ".cat-grid.btn-cmp-icon{position:absolute" in css
    assert "min-height:44px" in css.split(".cat-grid.btn-cmp-icon{")[1].split("}")[0]


def test_category_card_has_one_status_and_icon_compare(client_db):
    client, db, make = client_db
    buy = make([4000] * 12, name="Sony WH-1000XM5 černá", shop="alza", category="telefony")
    buy.category = "Mobilní telefony"
    buy.image_url = "https://img.example/wh.jpg"
    db.add(Offer(product=buy, shop="datart", url="https://www.datart.cz/wh-mobile",
                 price=4200, status="ok", in_stock=True))
    oos = make([9000], name="Bose tiché OOS", shop="alza", category="telefony")
    oos.category = "Mobilní telefony"
    oos.offers[0].in_stock = False
    db.commit()

    html = client.get("/kategorie/telefony").text

    def card_for(pid):
        start = html.rfind('<div class="card pcard">', 0, html.find(f'href="/produkt/{pid}-'))
        nxt = html.find('<div class="card pcard">', start + 1)
        return html[start: nxt if nxt != -1 else None]

    buy_card = card_for(buy.id)
    assert "btn-cmp-icon" in buy_card
    assert "✓ U minima" in buy_card
    assert "Kupuj – cena u minima" not in buy_card
    assert "u minima" not in buy_card.replace("✓ U minima", "")
    assert 'aria-label="Přidat do srovnání"' in buy_card
    assert "btn-cmp-icon" in buy_card
    assert "btn-cmp-quiet" not in buy_card
    assert "Do obchodu" in buy_card
    assert 'rel="nofollow noopener"' in buy_card or 'rel="nofollow sponsored noopener"' in buy_card
    assert "2 obchodů" in buy_card

    oos_card = card_for(oos.id)
    assert "btn-shop-sm" not in oos_card
    assert "Momentálně není skladem" in oos_card
    assert 'aria-label="Přidat do srovnání"' in oos_card
