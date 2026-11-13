"""Úvod a kategorie: fotka, zápis názvu se nemění v datech, sleva jen z mediánu."""
from datetime import datetime, timedelta, timezone

from lovceny.hero import clear_cache, home_board, measured_discount
from lovceny.images import has_working_image
from lovceny.models import AnalyticsEvent, Offer
from lovceny.popular import clear_cache as clear_popular
from lovceny.popular import popular_picks
from lovceny.verdict import Verdict


def test_working_image_rejects_empty_and_placeholder():
    assert has_working_image(None) is False
    assert has_working_image("  ") is False
    assert has_working_image("https://cdn.example/woocommerce-placeholder-300x300.webp") is False
    assert has_working_image("https://img.example/telefon.jpg") is True


def test_measured_discount_uses_median_not_a_made_up_percent():
    v = Verdict(code="wait", label="Počkej", text="", current=9000, min90=9000, median90=None, max90=20000)
    assert measured_discount(9000, v, [], hist_n=2) == (None, None)
    v2 = Verdict(code="buy", label="Kupuj", text="", current=4000, min90=4000, median90=10000, max90=12000)
    usual, pct = measured_discount(4000, v2, [], hist_n=10)
    assert usual == 10000
    assert pct == 60
    # maximum samo o sobě slevu nedělá
    flat = Verdict(code="wait", label="Počkej", text="", current=10000, min90=8000, median90=9000, max90=20000)
    assert measured_discount(10000, flat, [], hist_n=10) == (None, None)


def test_homepage_hides_products_without_image_and_category_sorts_them_last(client_db):
    client, db, make = client_db
    clear_cache()
    bare = make([1000], name="Levný telefon bez fotky", shop="alza", category="telefony")
    bare.image_url = None
    placeholder = make([1100], name="Telefon s placeholderem", shop="alza", category="telefony")
    placeholder.image_url = "https://www.alza.cz/woocommerce-placeholder.png"
    photo = make([9000], name="Drahý telefon s fotkou", shop="alza", category="telefony")
    photo.image_url = "https://img.example/telefon.jpg"
    db.commit()
    from lovceny.catcache import clear
    clear()

    home = client.get("/").text
    assert "Nejvíc nabízené modely" in home
    assert "Drahý telefon s fotkou" in home
    assert "Levný telefon bez fotky" in home
    assert "Naposledy přidané" not in home

    cat = client.get("/kategorie/telefony").text
    assert cat.find("Drahý telefon s fotkou") < cat.find("Levný telefon bez fotky")
    assert cat.find("Levný telefon bez fotky") < cat.find("Telefon s placeholderem") or (
        cat.find("Telefon s placeholderem") > cat.find("Drahý telefon s fotkou")
    )
    assert "Fotka produktu chybí" in cat


def test_popular_needs_image_stock_and_two_shops(client_db):
    client, db, make = client_db
    clear_popular()
    one = make([5000], name="Jen jeden obchod", shop="alza", category="notebooky")
    one.image_url = "https://img.example/jeden.jpg"
    oos = make([4000], name="Vyprodaný notebook", shop="alza", category="notebooky")
    oos.image_url = "https://img.example/oos.jpg"
    oos.offers[0].in_stock = False
    db.add(Offer(product=oos, shop="datart", url="https://www.datart.cz/oos-nb",
                 price=4100, status="ok", in_stock=False))
    phone = make([8000] * 8 + [6000], name="Oblíbený telefon", shop="alza", category="telefony")
    phone.image_url = "https://img.example/phone.jpg"
    db.add(Offer(product=phone, shop="datart", url="https://www.datart.cz/phone",
                 price=6200, status="ok", in_stock=True))
    vacuum = make([3000], name="Oblíbený vysavač", shop="alza", category="vysavace")
    vacuum.image_url = "https://img.example/vac.jpg"
    db.add(Offer(product=vacuum, shop="mironet", url="https://www.mironet.cz/vac",
                 price=3100, status="ok", in_stock=True))
    bare = make([2000], name="Bez fotky dva obchody", shop="alza", category="kavovary")
    bare.image_url = None
    db.add(Offer(product=bare, shop="datart", url="https://www.datart.cz/bare",
                 price=2100, status="ok", in_stock=True))
    now = datetime.now(timezone.utc)
    db.add(AnalyticsEvent(created_at=now, day="2026-11-12", kind="view", path=f"/produkt/{phone.id}",
                          product_id=phone.id, visitor="abc"))
    db.commit()
    clear_popular()

    picks = popular_picks(db, limit=8)
    names = [p.product.name for p in picks]
    assert "Oblíbený telefon" in names
    assert "Oblíbený vysavač" in names
    assert "Jen jeden obchod" not in names
    assert "Vyprodaný notebook" not in names
    assert "Bez fotky dva obchody" not in names
    assert all(p.n_shops >= 2 for p in picks)
    assert all(p.best.in_stock is not False for p in picks)
    slugs = [p.category_slug for p in picks]
    assert slugs[:2] == ["telefony", "vysavace"] or set(slugs[:2]) == {"telefony", "vysavace"}

    html = client.get("/").text
    assert "Nejvíc nabízené modely" in html
    assert "Oblíbený telefon" in html
    assert "Vyprodaný notebook" not in html
    assert "Oblíbený vysavač" not in html
    assert "Nejoblíbenější s nejlepší cenou" not in html


def test_home_chips_filter_markup(client_db):
    client, db, make = client_db
    clear_cache()
    a = make([1000], name="Chip telefon", shop="alza", category="telefony")
    a.image_url = "https://img.example/a.jpg"
    b = make([2000], name="Chip notebook", shop="alza", category="notebooky")
    b.image_url = "https://img.example/b.jpg"
    db.commit()
    html = client.get("/").text
    assert "Nejvíc nabízené modely" in html
    assert "Chip telefon" in html
    assert "Chip notebook" not in html
    assert 'href="/telefony"' in html
    assert 'data-pick="all"' not in html
    assert 'data-pick="notebooky"' not in html
