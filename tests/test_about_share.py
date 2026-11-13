"""Sdílení na detailu produktu, stránka O nás s FAQ (FAQPage JSON-LD) a kontaktní formulář."""
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

from lovceny.models import ContactMessage

ROOT = Path(__file__).parent.parent


def _ld(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


def test_share_button_on_product(client_db):
    client, _, make = client_db
    p = make([1200, 1000], name="Sdílený telefon Alfa")
    html = client.get(f"/produkt/{p.id}").text
    canon = f"https://lovceny.cz/produkt/{p.id}-{p.slug}"
    assert 'class="share"' in html and f'data-share-url="{canon}"' in html and "Kopírovat odkaz" in html
    assert "https://wa.me/?text=" in html and "fb-messenger://share/?link=" in html
    assert "https://x.com/intent/post?text=" in html and "mailto:?subject=" in html
    wa = re.search(r'href="(https://wa\.me/[^"]+)"', html).group(1)
    assert "Sdílený telefon Alfa" in unquote(wa) and "lovceny.cz" in unquote(wa)
    assert "share.js" in html


def test_no_third_party_scripts_anywhere(client_db):
    client, _, make = client_db
    p = make([1000])
    for path in ("/", f"/produkt/{p.id}", "/o-nas", "/stav", "/slevy", "/kategorie"):
        html = client.get(path).text
        srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
        assert all(s.startswith("/static/") for s in srcs), (path, srcs)
        assert "facebook.net" not in html and "platform.twitter.com" not in html and "googletagmanager" not in html


def test_about_page_content_and_faq_ld(client_db):
    client, _, _ = client_db
    r = client.get("/o-nas")
    assert r.status_code == 200
    html = r.text
    for s in ("Poctivé ceny", "Žádné placené pozice", "Jak vzniká verdikt", "Heureky a Zboží.cz", "Jak vyděláváme",
              "affiliate", "nikdy neovlivní", "Časté dotazy", "Napiš nám", "mailto:info@lovceny.cz"):
        assert s in html, s
    assert 'action="/kontakt"' not in html
    assert '<link rel="canonical" href="https://lovceny.cz/o-nas">' in html
    faq = next(d for d in _ld(html) if d.get("@type") == "FAQPage")
    assert len(faq["mainEntity"]) >= 6
    q = faq["mainEntity"][0]
    assert q["@type"] == "Question" and q["acceptedAnswer"]["@type"] == "Answer" and q["acceptedAnswer"]["text"]
    for item in faq["mainEntity"]:  # každá otázka z JSON-LD je i viditelně na stránce
        assert item["name"] in html


def test_nav_and_footer_links(client_db):
    client, _, _ = client_db
    html = client.get("/").text
    assert html.count('href="/o-nas"') >= 2 and 'href="/kontakt"' in html and 'href="/stav"' in html
    assert 'href="/o-nas#caste-dotazy"' in html


def test_sitemap_contains_about(client_db):
    client, _, _ = client_db
    assert "/o-nas</loc>" in client.get("/sitemap.xml").text


def test_contact_form(client_db):
    client, db, _ = client_db
    db.query(ContactMessage).delete()
    db.commit()
    r = client.post("/kontakt", data={"message": "Chybí mi obchod Kasa, šlo by přidat?", "topic": "obchod",
                                      "reply_to": "ja@example.cz", "name": "Jana"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/o-nas?odeslano=1#kontakt"
    m = db.query(ContactMessage).one()
    assert (m.topic, m.reply_to, m.name) == ("obchod", "ja@example.cz", "Jana")
    page = client.get("/o-nas?odeslano=1").text
    assert "mailto:info@lovceny.cz" in page and 'action="/kontakt"' not in page
    # krátká zpráva / špatný e-mail → chyba, nic se neuloží
    r = client.post("/kontakt", data={"message": "krátké"}, follow_redirects=False)
    assert "chyba=" in r.headers["location"]
    r = client.post("/kontakt", data={"message": "Dost dlouhá zpráva tady", "reply_to": "nene"}, follow_redirects=False)
    assert "chyba=" in r.headers["location"]
    # honeypot
    client.post("/kontakt", data={"message": "Spam spam spam spam", "website": "http://spam"})
    assert db.query(ContactMessage).count() == 1
    # neznámé téma → „jine“
    client.post("/kontakt", data={"message": "Další zpráva pro vás", "topic": "<x>"})
    assert db.query(ContactMessage).order_by(ContactMessage.id.desc()).first().topic == "jine"


def test_contact_rate_limit(client_db):
    from lovceny import main
    client, db, _ = client_db
    main._contact_rl.clear()
    for i in range(5):
        client.post("/kontakt", data={"message": f"Zpráva číslo {i} pro test"})
    r = client.post("/kontakt", data={"message": "Šestá zpráva za hodinu"}, follow_redirects=False)
    assert "chyba=" in r.headers["location"]
    main._contact_rl.clear()


def test_private_email_never_public():
    """Soukromý e-mail autora nesmí být v kódu, šablonách ani v historii commitů repozitáře."""
    needle = "karel.matusiak@" + "proton"
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    for f in tracked:
        p = ROOT / f
        if p.is_file() and p.suffix in {".py", ".html", ".js", ".css", ".md", ".yml", ".yaml", ".json", ".txt", ".toml", ".svg"}:
            assert needle not in p.read_text(errors="ignore"), f
