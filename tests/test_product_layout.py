"""Detail: velká fotka, jeden ověřovací blok, tip kdy nakupovat až dole."""


def test_product_layout_css_and_order(client_db):
    client, db, make = client_db
    p = make([5000] * 10, name="Sony WH-1000XM5", shop="alza", category="telefony")
    p.image_url = "https://img.example/wh.jpg"
    html = client.get(f"/produkt/{p.id}-{p.slug}").text
    assert 'class="p-top"' in html and 'class="verify-card' in html
    assert html.find('class="verify-card') < html.find('class="p-chart"')
    assert html.find('class="offers"') < html.find("season-hint")
    assert 'href="/go/' in html
    assert 'rel="nofollow' in html
    css = open("lovceny/static/app.css", encoding="utf-8").read()
    assert "-webkit-line-clamp:3" in css
    assert "grid-template-columns:360px" in css.replace(" ", "")
    oos = make([9000], name="Bose vyprodaný detail", shop="alza", category="telefony")
    oos.offers[0].in_stock = False
    db.commit()
    page = client.get(f"/produkt/{oos.id}-{oos.slug}").text
    assert "btn-buy" not in page and "Koupit za" not in page
