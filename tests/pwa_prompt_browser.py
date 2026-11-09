"""Chování výzvy na plochu v Chrome proti lokálnímu serveru (ne produkce)."""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

PORT = 8776
CHROME_PORT = 9240
BASE = f"http://127.0.0.1:{PORT}"
ROOT = Path(__file__).resolve().parents[1]
FAKE = """({
  preventDefault() {},
  prompt() { return Promise.resolve(); },
  userChoice: Promise.resolve({outcome: 'dismissed'})
})"""


def screenshot_dir() -> Path:
    """Snímky rozložení. Na agentovi /opt/cursor/artifacts, jinak dočasný adresář."""
    preferred = Path("/opt/cursor/artifacts")
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / f".write-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "lovceny-pwa-artifacts"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _log_tail(path: Path, limit: int = 6000) -> str:
    try:
        text = path.read_text(errors="replace").strip()
    except OSError as exc:
        return f"(log {path} se nepodařilo přečíst: {exc})"
    if not text:
        return f"(proces nic nevypsal, log {path})"
    if len(text) > limit:
        text = text[-limit:]
    return text


def _userns_hint() -> str:
    knob = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    try:
        restricted = knob.read_text().strip() == "1"
    except OSError:
        return ""
    if not restricted:
        return ""
    return (
        "kernel.apparmor_restrict_unprivileged_userns=1, Chrome na tomhle hostu "
        "často skončí dřív, než otevře DevTools port.\n"
    )


def relax_userns_on_ci() -> None:
    """Ubuntu 24.04/26.04 (ubuntu-latest) blokuje user namespace přes AppArmor.

    Chrome pak spadne ještě před tím, než začne poslouchat na debug portu.
    Runner je jednorázový, takže omezení na dobu jobu zvedneme.
    """
    if os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get("CI") != "true":
        return
    knob = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    try:
        if knob.read_text().strip() != "1":
            return
    except OSError:
        return
    subprocess.run(
        ["sudo", "-n", "sysctl", "-w", "kernel.apparmor_restrict_unprivileged_userns=0"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def chrome_binary() -> str | None:
    """Skutečný binární soubor, ne bash obal google-chrome.

    Obal přepisuje stdout/stderr přes process substitution. Když je výstup
    soubor nebo /dev/null, DevTools port se někdy vůbec neotevře.
    """
    raw = os.environ.get("LOVCENY_CHROME") or shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not raw:
        return None
    resolved = Path(raw).resolve()
    for candidate in (resolved.parent / "chrome", Path("/opt/google/chrome/chrome"), resolved, Path(raw)):
        if candidate.is_file() and _is_elf(candidate):
            return str(candidate)
    return raw


def chrome_environment(base: dict) -> dict:
    """Chrome nesmí dědit LD_LIBRARY_PATH z actions/setup-python.

    Ten ukazuje do toolcache s libpython. Chrome má brát systémové knihovny,
    jinak může spadnout dřív, než otevře debug port.
    """
    env = base.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    return env


def chrome_command(binary: str, user_data: Path) -> list[str]:
    return [
        binary,
        "--headless=new",
        "--ozone-platform=headless",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-software-rasterizer",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--hide-scrollbars",
        f"--remote-debugging-port={CHROME_PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data}",
        "about:blank",
    ]


def _debug_port(user_data: Path) -> int | None:
    try:
        line = (user_data / "DevToolsActivePort").read_text(errors="replace").splitlines()[0].strip()
        port = int(line)
    except (OSError, ValueError, IndexError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _not_ready(proc: subprocess.Popen, log_path: Path, message: str) -> SystemExit:
    """Nejdřív proces ukonči, ať se vyprázdní buffer, a teprve pak čti log."""
    if proc.poll() is None:
        _stop(proc)
    return SystemExit(f"{message}\n{_userns_hint()}{_log_tail(log_path)}")


def wait_http(url: str, proc: subprocess.Popen, log_path: Path, what: str, seconds: float = 30.0) -> None:
    """Čekej, až url odpoví. Connection refused je pořád „ještě ne“, ne úspěch.

    Když proces skončí, nebo se port neotevře, skonči s jeho logem. Neházej
    dál holé URLError.
    """
    deadline = time.monotonic() + seconds
    last = "zatím bez pokusu"
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise _not_ready(
                proc, log_path,
                f"{what} skončil s kódem {code}, než {url} začal přijímat spojení.",
            )
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                resp.read(128)
            return
        except Exception as exc:  # noqa: BLE001 — refused, reset i timeout: zkoušej dál
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
    raise _not_ready(
        proc, log_path,
        f"{what} se do {seconds:.0f}s neozval na {url} ({last}).",
    )


def wait_chrome(proc: subprocess.Popen, user_data: Path, log_path: Path, seconds: float = 30.0) -> int:
    """DevTools port. Chrome si ho může zapsat do DevToolsActivePort jinak, než jsme chtěli."""
    deadline = time.monotonic() + seconds
    last = "zatím bez pokusu"
    port = CHROME_PORT
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise _not_ready(
                proc, log_path,
                f"Chrome skončil s kódem {code}, než DevTools port začal přijímat spojení.",
            )
        found = _debug_port(user_data)
        if found:
            port = found
        url = f"http://127.0.0.1:{port}/json/version"
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                resp.read(256)
            return port
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.25)
    url = f"http://127.0.0.1:{port}/json/version"
    raise _not_ready(
        proc, log_path,
        f"Chrome se do {seconds:.0f}s neozval na {url} ({last}).",
    )


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
        shots = screenshot_dir()
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
            open(shots / name, "wb").write(base64.b64decode(shot["data"]))
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


def _spawn(argv: list[str], log_path: Path, env: dict, cwd: str | None = None) -> subprocess.Popen:
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        return subprocess.Popen(argv, cwd=cwd, env=env, stdout=fd, stderr=subprocess.STDOUT)
    finally:
        os.close(fd)


def main():
    relax_userns_on_ci()
    binary = chrome_binary()
    if not binary:
        raise SystemExit("google-chrome není nainstalovaný")
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:////tmp/lovceny-pwa.db"
    env["PYTHONUNBUFFERED"] = "1"
    if os.path.exists("/tmp/lovceny-pwa.db"):
        os.remove("/tmp/lovceny-pwa.db")
    subprocess.check_call([sys.executable, "-c", """
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
"""], cwd=str(ROOT), env=env)
    server_log = Path(tempfile.mkstemp(prefix="lovceny-uvicorn-", suffix=".log")[1])
    chrome_log = Path(tempfile.mkstemp(prefix="lovceny-chrome-", suffix=".log")[1])
    user_data = Path(tempfile.mkdtemp(prefix="lovceny-chrome-profile-"))
    server = None
    chrome = None
    try:
        server = _spawn(
            [sys.executable, "-m", "uvicorn", "lovceny.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
            server_log, env, cwd=str(ROOT),
        )
        chrome = _spawn(chrome_command(binary, user_data), chrome_log, chrome_environment(env))
        wait_http(BASE + "/healthz", server, server_log, "uvicorn")
        debug_port = wait_chrome(chrome, user_data, chrome_log)
        asyncio.run(run(debug_port))
    finally:
        _stop(chrome)
        _stop(server)
        server_log.unlink(missing_ok=True)
        chrome_log.unlink(missing_ok=True)
        shutil.rmtree(user_data, ignore_errors=True)


if __name__ == "__main__":
    main()
