"""Karty ve stejném řádku drží cenu a tlačítka na společné základně. Text, CTA a affiliate se nemění."""
from lovceny.models import Offer


def _card(html: str, pid: int) -> str:
    marker = f'class="pcard-link" href="/produkt/{pid}-'
    start = html.find(marker)
    assert start != -1, pid
    open_at = html.rfind('<div class="card pcard">', 0, start)
    assert open_at != -1
    nxt = html.find('<div class="card pcard">', open_at + 1)
    return html[open_at: nxt if nxt != -1 else None]


def test_card_alignment_css_contract():
    css = open("lovceny/static/app.css", encoding="utf-8").read()
    compact = css.replace(" ", "").replace("\n", "")
    assert "grid-template-rows:subgrid" in compact
    assert "grid-row:span3" in compact
    assert ".grid>.pcard>.pcard-link{display:contents" in compact
    # verdikt „Sbíráme data“ dál nesmí duplikovat důvod na kartě
    assert ".pcard .v-none .verdict-reason{display:none}" in css


def test_shared_card_rows_keep_cta_and_full_title(client_db):
    c, db, make = client_db
    long_name = (
        "Sony bezdrátová sluchátka WH-1000XM5 s velmi dlouhým českým názvem "
        "pro kontrolu zalomení řádku a zarovnání ceny i tlačítka"
    )
    buy = make([8990] * 12, name=long_name, shop="mironet", category="telefony")
    buy.category = "Mobilní telefony"
    thin = make([534], name="JBL Tune 500 černá", shop="xm", category="telefony")
    thin.category = "Mobilní telefony"
    db.add(Offer(
        product=thin, shop="allegro", url="https://allegro.cz/nabidka/jbl-tune-500-align",
        price=707, status="ok", in_stock=True,
    ))
    oos = make([12000], name="Bose QuietComfort Ultra OOS", shop="alza", category="telefony")
    oos.offers[0].in_stock = False
    db.commit()

    html = c.get("/kategorie/telefony").text
    buy_card = _card(html, buy.id)
    thin_card = _card(html, thin.id)
    oos_card = _card(html, oos.id)

    for card in (buy_card, thin_card, oos_card):
        assert 'class="p-head"' in card
        assert 'class="pc-row"' in card
        assert 'class="pc-prices"' in card
        assert 'class="pcard-act"' in card
        assert "Přidat do srovnání" in card
        assert 'class="p-name sm"' in card

    assert long_name in buy_card
    assert f'title="{long_name}"' in buy_card
    assert "Do obchodu" in buy_card
    assert 'class="btn btn-shop-sm"' in buy_card
    assert 'rel="nofollow noopener"' in buy_card
    assert "2 obchodů" in thin_card
    assert "Do obchodu" in thin_card
    assert "btn-shop-sm" not in oos_card
    assert "Momentálně není skladem" in oos_card
    assert "Přidat do srovnání" in oos_card
