"""Viditelný název na kartě. Uložený name, slug, hledání ani JSON-LD se nemění."""
from types import SimpleNamespace

from lovceny.names import card_label, short_name
from lovceny.seo import product_ld
from lovceny.service import search_db
from lovceny.util import slugify


def test_headphone_feed_titles_lead_with_model():
    raw = "Originální Drátová Sluchátka Jbl Tune 500 Na Uši S Mikrofonem, Černá"
    label = card_label(raw)
    assert label.startswith("Jbl Tune 500")
    assert "Originální" in label and "Černá" in label and "Na Uši" in label
    assert "JBL" not in label  # zdroj píše Jbl, nepřepisovat
    assert "sluchátka" not in label.casefold()

    buried = "Bezdrátová sluchátka T92 Anc Pro dokovací stanice černá"
    assert card_label(buried).startswith("T92")
    assert "Anc" in card_label(buried) and "černá" in card_label(buried)

    generic = "Bezdrátová Sluchátka Bluetooth 5.3 Anc Enc Dokanálový Mikrofon Černá"
    assert card_label(generic) == "Bezdrátová Bluetooth 5.3 Anc Enc Dokanálový Mikrofon Černá"
    assert "ANC" not in card_label(generic)  # zůstane Anc, jak je ve feedu


def test_keeps_condition_color_capacity_generation_and_acronyms():
    assert card_label("Repasovaná sluchátka Sony WH-CH720N černá").startswith("Sony WH-CH720N")
    assert "Repasovaná" in card_label("Repasovaná sluchátka Sony WH-CH720N černá")
    compat = card_label("Kompatibilní sluchátka k iPhone USB-C bílá")
    assert compat.startswith("Kompatibilní")
    assert "USB-C" in compat and "iPhone" in compat
    assert card_label("Bazarová sluchátka JBL Tune 510BT černá") == "JBL Tune 510BT černá Bazarová"
    phone = "SAMSUNG Galaxy A56 5G 8+128GB černá / 6.7\" / Android 15"
    short = card_label(phone)
    assert "A56" in short and "128GB" in short and "černá" in short
    assert "Android" not in short  # výčet specifikací dál řeší short_name
    assert card_label("Apple iPhone 16 128GB - černý (2024)") == "Apple iPhone 16 128GB černý (2024)"
    sony = card_label("ANC USB-C Sony WH-CH720N")
    assert sony.startswith("Sony WH-CH720N")
    assert "ANC" in sony and "USB-C" in sony


def test_other_categories_and_missing_brand():
    assert card_label("Pračka Gorenje WG2P94A22W") == "Gorenje WG2P94A22W"
    assert card_label("65\" TCL 65T69C titanová").startswith("TCL 65T69C")
    assert "titanová" in card_label("65\" TCL 65T69C titanová")
    notebook = "Nový Model Notebook Hp 17-cn Intel i5-13 Ssd 512GB Podsvícená klávesnice"
    assert card_label(notebook).startswith("Hp 17-cn")
    assert "512GB" in card_label(notebook)
    assert "Nový Model" not in card_label(notebook)
    generic_watch = "Sportovní Hodinky Chytré Hodinky S Voláním Pánské Dámské Polské Menu Černé"
    assert card_label(generic_watch) == generic_watch
    short_already = "JBL Tune 500 černá"
    assert card_label(short_already) == short_already
    # lomítkový výčet se nevrací zpátky
    mac = 'Apple MacBook Air 15" M3 2024 CZ Silver / Apple M3 4.0GHz / 16GB / 512GB / macOS'
    assert card_label(mac) == short_name(mac)
    assert "GHz" not in card_label(mac) and "16GB" in card_label(mac)


def test_does_not_invent_or_retitle_case():
    assert card_label("Dkny Bluetooth sluchátka do uší černá/černá Checkered Pattern").startswith("Dkny")
    assert "černá/černá" not in card_label("Dkny Bluetooth sluchátka do uší černá/černá Checkered Pattern")
    assert "Checkered Pattern" in card_label("Dkny Bluetooth sluchátka do uší černá/černá Checkered Pattern")
    # bez značky i kódu se nic nepředstírá
    coffee = "pákový tlakový kávovar Srebny 20 Bar 500 1300 W"
    assert card_label(coffee).startswith("pákový")


def test_card_keeps_stored_identity_search_and_escapes(client_db):
    client, db, make = client_db
    raw = "Originální Drátová Sluchátka Jbl Tune 500 Na Uši S Mikrofonem, Černá"
    xss = '<img src=x onerror="alert(1)"> Sony WH-CH720N'
    jbl = make([1990], name=raw, shop="alza", category="sluchatka")
    jbl.category = "Sluchátka"
    nasty = make([890], name=xss, shop="alza", category="sluchatka")
    nasty.category = "Sluchátka"
    plain = make([500], name="JBL Tune 500 černá", shop="alza", category="sluchatka")
    db.commit()

    assert jbl.name == raw
    assert jbl.slug == slugify(raw)
    assert search_db(db, "Originální Drátová")[0].id == jbl.id

    html = client.get("/kategorie/sluchatka").text
    assert "Jbl Tune 500 Originální" in html
    assert f'title="{raw}"' in html
    assert "Celý název:" in html and raw in html
    assert 'onerror="alert(1)"' not in html
    assert "&lt;img" in html
    assert "JBL Tune 500 černá" in html

    detail = client.get(f"/produkt/{jbl.id}-{jbl.slug}").text
    assert raw in detail
    ld = product_ld(SimpleNamespace(
        id=jbl.id, slug=jbl.slug, name=jbl.name, display_name=short_name(jbl.name),
        image_url=None, ean=None, category_slug="sluchatka", category="Sluchátka",
    ), [])
    assert ld["name"] == short_name(raw)
    assert ld["name"] != card_label(raw)
    assert plain.name == "JBL Tune 500 černá"
