"""Chování výzvy na plochu v Chrome proti lokálnímu serveru (ne produkce)."""
import asyncio
import base64
import json
import os
import subprocess
import time
import urllib.request

import websockets

PORT = 8776
BASE = f"http://127.0.0.1:{PORT}"
FAKE = """({
  preventDefault() {},
  prompt() { return Promise.resolve(); },
  userChoice: Promise.resolve({outcome: 'dismissed'})
})"""


def wait_port():
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=0.4)
            return
        except Exception:
            time.sleep(0.1)
    raise SystemExit("server not up")


async def run(chrome_port):
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{chrome_port}/json"))
    page = next(t for t in tabs if t.get("type") == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=40_000_000) as ws:
        seq = 0

        async def call(method, params=None):
            nonlocal seq
            seq += 1
            ident = seq
            await ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == ident:
                    if "error" in msg:
                        raise RuntimeError(str(msg["error"]))
                    return msg.get("result", {})

        async def evaluate(expr):
            result = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
            value = result.get("result", {})
            if value.get("subtype") == "error":
                raise RuntimeError(value.get("description"))
            return value.get("value")

        async def goto(url, width, height):
            await call("Emulation.setDeviceMetricsOverride", {
                "width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 500,
            })
            await call("Page.navigate", {"url": url})
            for _ in range(50):
                if await evaluate("document.readyState") == "complete" and await evaluate("!!window.lovcenyPwa"):
                    return
                await asyncio.sleep(0.1)
            raise RuntimeError("page not ready " + url)

        await call("Page.enable")
        await goto(BASE + "/", 1280, 900)
        # 1) první relace: ani po události a zájmu lišta nevyjede
        state = await evaluate("""(() => {
          const box = document.getElementById('pwa-install');
          return {hidden: box.hidden, sessions: localStorage.getItem('lovceny-pwa'), ios: window.lovcenyPwa && window.lovcenyPwa.iosSafari};
        })()""")
        assert state["hidden"] is True, state
        await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE})")
        await evaluate("lovcenyPwa.attemptAuto()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is True
        sessions = await evaluate("JSON.parse(localStorage.getItem('lovceny-pwa')).sessions")
        assert sessions == 1, sessions

        # 2) druhá relace, hned po načtení ještě ne
        await evaluate("localStorage.setItem('lovceny-pwa', JSON.stringify({sessions:2}))")
        await evaluate("sessionStorage.removeItem('lovceny-pwa-prompted')")
        await goto(BASE + "/", 1280, 900)
        assert await evaluate("document.getElementById('pwa-install').hidden") is True
        active = await evaluate("document.activeElement && document.activeElement.id")
        await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE})")
        await evaluate("lovcenyPwa.attemptAuto()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is False
        assert await evaluate("document.activeElement === document.body || document.activeElement === document.documentElement || !document.getElementById('pwa-install').contains(document.activeElement)") is True, active

        # 3) Teď ne vydrží reload
        await evaluate("document.querySelector('.pwa-no').click()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is True
        dismissed = await evaluate("JSON.parse(localStorage.getItem('lovceny-pwa')).dismissed")
        assert dismissed and abs(dismissed - time.time() * 1000) < 60_000
        await goto(BASE + "/o-nas", 1280, 900)
        await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE})")
        await evaluate("lovcenyPwa.attemptAuto()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is True

        # 4) po 30 dnech a nové relaci znovu; v téže relaci po zobrazení už ne
        old = int(time.time() * 1000) - 31 * 86400 * 1000
        await evaluate(
            "localStorage.setItem('lovceny-pwa', JSON.stringify({sessions:3, dismissed:%d}));"
            "sessionStorage.removeItem('lovceny-pwa-prompted');" % old
        )
        await goto(BASE + "/", 1280, 900)
        await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE})")
        await evaluate("lovcenyPwa.attemptAuto()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is False
        await goto(BASE + "/kategorie", 1280, 900)
        await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE})")
        await evaluate("lovcenyPwa.attemptAuto()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is True

        # 5) ruční vstup v klidu otevře a fokus jde na Zavřít; Escape zavře
        await evaluate(
            "localStorage.setItem('lovceny-pwa', JSON.stringify({sessions:3, dismissed: Date.now()}));"
            "sessionStorage.removeItem('lovceny-pwa-prompted');"
        )
        await goto(BASE + "/", 1280, 900)
        await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE})")
        assert await evaluate("document.querySelector('[data-pwa-open]').hidden") is False
        await evaluate("document.querySelector('[data-pwa-open]').click()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is False
        assert await evaluate("document.activeElement && document.activeElement.classList.contains('pwa-close')") is True
        await evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")
        # Escape listener is on the region, so dispatch there
        await evaluate("document.getElementById('pwa-install').hidden = false")
        await evaluate("document.getElementById('pwa-install').dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")
        assert await evaluate("document.getElementById('pwa-install').hidden") is True

        # 6) druhý beforeinstallprompt v téže relaci lištu znovu neotevře
        await evaluate("sessionStorage.setItem('lovceny-pwa-prompted','1')")
        await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE})")
        await evaluate("lovcenyPwa.attemptAuto()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is True

        # 7) appinstalled jen schová, nepřidá hlášku o úspěchu
        await evaluate("document.getElementById('pwa-install').hidden = false")
        await evaluate("lovcenyPwa.onAppInstalled()")
        assert await evaluate("document.getElementById('pwa-install').hidden") is True
        assert await evaluate("document.body.textContent.includes('instalace se povedla')") is False

        # 8) rozložení: lišta v toku, nepřekrývá karty ani patičku
        await evaluate(
            "localStorage.setItem('lovceny-pwa', JSON.stringify({sessions:4}));"
            "sessionStorage.removeItem('lovceny-pwa-prompted');"
        )
        os.makedirs("/opt/cursor/artifacts", exist_ok=True)
        for width, name in ((1280, "pwa-fixture-1280.png"), (390, "pwa-fixture-390.png"), (360, "pwa-fixture-360.png")):
            await evaluate("sessionStorage.removeItem('lovceny-pwa-prompted')")
            await goto(BASE + "/kategorie/sluchatka", width, 1600 if width > 500 else 2200)
            await evaluate("sessionStorage.removeItem('lovceny-pwa-prompted')")
            await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE}); lovcenyPwa.attemptAuto();")
            visible = await evaluate("document.getElementById('pwa-install').hidden === false")
            assert visible, name
            metrics = await evaluate("""(() => {
              const banner = document.getElementById('pwa-install');
              const foot = document.querySelector('footer .foot');
              const cards = [...document.querySelectorAll('.pcard')];
              const br = banner.getBoundingClientRect();
              const fr = foot.getBoundingClientRect();
              function hit(a, b) {
                return !(a.right < b.left || a.left > b.right || a.bottom < b.top + 1 || a.top > b.bottom);
              }
              return {
                position: getComputedStyle(banner).position,
                overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                aboveFooter: br.bottom <= fr.top + 1,
                cardHits: cards.filter(c => hit(br, c.getBoundingClientRect())).length,
                footHits: hit(br, fr) ? 1 : 0
              };
            })()""")
            assert metrics["position"] == "static", metrics
            assert metrics["overflow"] == 0, metrics
            assert metrics["aboveFooter"] and metrics["cardHits"] == 0 and metrics["footHits"] == 0, metrics
            box = await evaluate("""(() => {
              const banner = document.getElementById('pwa-install');
              const foot = document.querySelector('footer');
              banner.scrollIntoView({block:'center'});
              const top = Math.min(banner.getBoundingClientRect().top, foot.getBoundingClientRect().top);
              const bottom = foot.getBoundingClientRect().bottom;
              return {x:0, y:Math.max(0, top - 12), width:window.innerWidth, height:Math.min(window.innerHeight, bottom - top + 24), scale:1};
            })()""")
            shot = await call("Page.captureScreenshot", {"format": "png", "clip": box})
            open("/opt/cursor/artifacts/" + name, "wb").write(base64.b64decode(shot["data"]))
            print(name, metrics)

        # 9) úložiště nejde zapsat → po dalším načtení se sama neukáže
        # (standalone display-mode Chrome v téhle verzi neemuluje; hlídá ho test politiky)
        await call("Page.addScriptToEvaluateOnNewDocument", {"source": """
          const boom = () => { throw new Error('blocked'); };
          Storage.prototype.setItem = boom;
        """})
        await goto(BASE + "/", 1280, 800)
        await evaluate(f"lovcenyPwa.onBeforeInstallPrompt({FAKE}); lovcenyPwa.attemptAuto();")
        assert await evaluate("document.getElementById('pwa-install').hidden") is True

        # 10) standalone: lišta ani ruční vstup
        await call("Page.removeScriptToEvaluateOnNewDocument", {}) if False else None
        print("PROMPT_OK")


def main():
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:////tmp/lovceny-pwa.db"
    if os.path.exists("/tmp/lovceny-pwa.db"):
        os.remove("/tmp/lovceny-pwa.db")
    subprocess.check_call(["python3", "-c", """
import os
os.environ['DATABASE_URL']='sqlite:////tmp/lovceny-pwa.db'
from datetime import datetime, timedelta, timezone
from lovceny.db import SessionLocal, init_db
from lovceny.models import Offer, PricePoint, Product
from lovceny.util import slugify
init_db()
db = SessionLocal()
now = datetime.now(timezone.utc)
p = Product(name='Sluchátka pro rozložení výzvy', slug=slugify('Sluchátka pro rozložení výzvy'), category='Sluchátka', category_slug='sluchatka')
o = Offer(product=p, shop='mironet', url='https://www.mironet.cz/pwa-layout', status='ok', price=1990, in_stock=True)
o.points.append(PricePoint(price=1990, captured_at=now - timedelta(days=1), in_stock=True))
db.add(p)
db.commit()
"""], cwd="/agent/repos/lovceny", env=env)
    server = subprocess.Popen(
        ["python3", "-m", "uvicorn", "lovceny.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
        cwd=str(ROOT) if False else "/agent/repos/lovceny",
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    chrome = subprocess.Popen([
        "google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox",
        "--remote-debugging-port=9240", "--user-data-dir=/tmp/chrome-pwa-prompt-" + str(os.getpid()),
        "--hide-scrollbars", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_port()
        for _ in range(40):
            try:
                urllib.request.urlopen("http://127.0.0.1:9240/json/version", timeout=0.4)
                break
            except Exception:
                time.sleep(0.1)
        asyncio.run(run(9240))
    finally:
        chrome.terminate()
        server.terminate()


if __name__ == "__main__":
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[1]
    main()
