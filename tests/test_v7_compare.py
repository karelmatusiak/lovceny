"""v7 – Srovnání produktů: /porovnat?p=ID-slug,… (2–3 produkty), sdílitelná URL, noindex, SVG graf, parametry."""
from lovceny.specs import extract, table


def test_porovnat_by_tokens_and_slugs(client_db):
    client, db, make = client_db
    a = make([12990] * 5 + [11990] * 5, name="Samsung QE55Q60D 55\" QLED srovnani")
    b = make([13990] * 10, name="LG OLED55B42 55\" OLED 120Hz srovnani", shop="softcom")
    c = make([9990] * 10, name="TCL 55C655 QLED srovnani treti")
    h = client.get(f"/porovnat?p={a.id}-{a.slug},{b.slug},{c.id}").text
    assert h.count('class="card cmp-col') == 3
    assert '<meta name="robots" content="noindex, follow">' in h and 'rel="canonical"' not in h
    assert "Nejnižší cena ve srovnání" in h and "nejlevnější" in h  # c je nejlevnější
    assert "Nejnižší cena za 30 dní" in h and "Obvyklá cena" in h and "Sledujeme" in h
    assert "Klíčové parametry" in h and "Úhlopříčka" in h and "OLED" in h and "120 Hz" in h
    assert h.count('class="chart chart-multi"') >= 2 and 'stroke-dasharray="7 5"' in h  # 2. čára i vzorem
    assert f"/porovnat?p={a.id}-{a.slug},{b.id}-{b.slug},{c.id}-{c.slug}" in h  # sdílitelná URL v Sdílet
    assert "Srovnání: " in h and " vs. " in h and "Sdílet srovnání" in h


def test_porovnat_max_three_dedup_and_junk(client_db):
    client, db, make = client_db
    ps = [make([1000 + i] * 3, name=f"Srovnani limit {i}") for i in range(4)]
    raw = ",".join(f"{p.id}-{p.slug}" for p in ps) + f",{ps[0].id},<script>,../../etc"
    h = client.get(f"/porovnat?p={raw}").text
    assert h.count('class="card cmp-col') == 3 and "<script>," not in h


def test_legacy_redirect_and_pridat(client_db):
    client, db, make = client_db
    a = make([500] * 3, name="Srovnani legacy A")
    b = make([600] * 3, name="Srovnani legacy B")
    r = client.get(f"/porovnani?ids={a.id},{b.id}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/porovnat?p={a.id}-{a.slug},{b.id}-{b.slug}"
    r = client.get(f"/porovnani?pridat={b.id}", follow_redirects=False)
    assert r.headers["location"] == f"/porovnat?pridat={b.id}"
    h = client.get(f"/porovnat?p={a.id}&pridat={b.id}").text
    assert h.count('class="card cmp-col') == 2


def test_buttons_robots_sitemap(client_db):
    client, db, make = client_db
    a = make([700] * 3, name="Srovnani tlacitko")
    d = client.get(f"/produkt/{a.id}-{a.slug}").text
    assert f'data-cmp-id="{a.id}" data-cmp-slug="{a.slug}"' in d and "Přidat do srovnání" in d
    assert "/porovnat" not in client.get("/sitemap.xml").text
    assert "Disallow: /porovnat" not in client.get("/robots.txt").text  # crawler musí vidět noindex


def test_specs_from_name_only():
    assert extract("Xiaomi Redmi Note 13 5G 8GB/256GB černá") == {"ram": "8 GB", "storage": "256 GB", "net": "5G", "color": "černá"}
    assert extract("Bosch WGG244ZCBY 9 kg 1400 ot.")["load"] == "9 kg"
    assert extract("Sony WH-1000XM5") == {}
    rows = table(["iPhone 16 128GB", "Galaxy S25 256GB"])
    assert rows == [("Úložiště", ["128 GB", "256 GB"])]
