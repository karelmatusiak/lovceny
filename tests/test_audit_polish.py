"""Úklid surových názvů z feedu, 301 starého slugu a titulek stránkování."""
import re

from lovceny.names import canonical_slug, card_label
from lovceny.seo import robots_txt, website_ld
from lovceny.util import slugify


def test_feed_titles_drop_shop_prefix_and_marketing():
    phone = "Smartphone Xiaomi Redmi 15 4G 8/256GB Dual Sim Nfc Titanově šedý"
    assert card_label(phone) == "Xiaomi Redmi 15 4G 8/256GB Titanově šedý"

    tune290 = "Jbl Tune 290 Originální In-ear Sluchátka T290 Černé Nové By Harman"
    assert card_label(tune290) == "JBL Tune 290 Černé"
    assert "Harman" not in card_label(tune290) and "T290" not in card_label(tune290)

    tune500 = "Originální Drátová Sluchátka Jbl Tune 500 Na Uši S Mikrofonem, Černá"
    assert card_label(tune500) == "JBL Tune 500, Černá"

    quoted = '55\\" LED Samsung OM55N-S FHD 4000cd/mi SO 24/7'
    assert card_label(quoted) == 'Samsung OM55N-S FHD 55"'
    commas = "Samsung OM55N-S FHD,4000cd, MI, 55'' LED SO,24/7"
    assert card_label(commas) == 'Samsung OM55N-S FHD 55"'

    # Podmínka, barva a už čistý název zůstávají. Značku nedomýšlíme.
    assert card_label("Repasovaná sluchátka Sony WH-CH720N černá").startswith("Sony WH-CH720N")
    assert "Repasovaná" in card_label("Repasovaná sluchátka Sony WH-CH720N černá")
    assert card_label("JBL Tune 500 černá") == "JBL Tune 500 černá"
    assert card_label("JBL Tune 500 JBL černá") == "JBL Tune 500 černá"
    assert card_label("Samsung Galaxy A55 128GB") == "Samsung Galaxy A55 128GB"
    assert "QLED" in card_label('Samsung QE43LS03FAUXXH 43" QLED')


def test_changed_slug_redirects_and_hand_slug_stays(client_db):
    client, db, make = client_db
    raw = "Jbl Tune 290 Originální In-ear Sluchátka T290 Černé Nové By Harman"
    product = make([1161], name=raw, shop="alza", category="telefony")
    db.commit()
    assert product.slug == slugify(raw)
    public = canonical_slug(product.name, product.slug)
    assert public == "jbl-tune-290-cerne"
    assert public != product.slug

    old = client.get(f"/produkt/{product.id}-{product.slug}", follow_redirects=False)
    assert old.status_code == 301
    assert old.headers["location"] == f"/produkt/{product.id}-{public}"
    page = client.get(f"/produkt/{product.id}-{public}", follow_redirects=False)
    assert page.status_code == 200
    assert "JBL Tune 290 Černé" in page.text
    assert f'rel="canonical" href="https://lovceny.cz/produkt/{product.id}-{public}"' in page.text
    wrong = client.get(f"/produkt/{product.id}-jiny-slug", follow_redirects=False)
    assert wrong.status_code == 301
    assert wrong.headers["location"] == f"/produkt/{product.id}-{public}"

    kept = make([8000], name="Canonical Phone", category="telefony")
    kept.slug = "rucni-slug"
    db.commit()
    assert canonical_slug(kept.name, kept.slug) == "rucni-slug"
    same = client.get(f"/produkt/{kept.id}-rucni-slug", follow_redirects=False)
    assert same.status_code == 200


def test_pagination_title_names_the_page_and_canonical_stays(client_db):
    client, db, make = client_db
    from lovceny.models import Product
    from lovceny.catcache import clear
    db.query(Product).filter(Product.category_slug == "telefony").delete()
    db.commit()
    clear()
    for i in range(25):
        make([1000 + i], name=f"Samsung Galaxy A{i:02d} stránka", shop="alza", category="telefony")
    first = client.get("/kategorie/telefony")
    second = client.get("/kategorie/telefony", params={"strana": 2})
    title1 = first.text.split("<title>", 1)[1].split("</title>", 1)[0]
    title2 = second.text.split("<title>", 1)[1].split("</title>", 1)[0]
    assert "– strana" not in title1
    assert "– strana 2 |" in title2
    desc2 = re.search(r'<meta name="description" content="([^"]*)"', second.text).group(1)
    assert desc2.endswith("Strana 2.") or "Strana 2." in desc2
    assert 'rel="canonical" href="https://lovceny.cz/kategorie/telefony"' in first.text
    assert 'rel="canonical" href="https://lovceny.cz/kategorie/telefony?strana=2"' in second.text
    assert "strana" not in re.search(r'<h1 class="page-h1">(.*?)</h1>', second.text).group(1).casefold()


def test_search_action_is_not_advertised_on_a_blocked_url():
    site = website_ld()
    assert site["@type"] == "WebSite"
    assert "potentialAction" not in site
    assert "Disallow: /hledat" in robots_txt()
