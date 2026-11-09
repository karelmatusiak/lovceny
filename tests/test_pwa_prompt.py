"""Politika výzvy na plochu: klid, relace, úložiště. Chování v prohlížeči, když je Chrome."""
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
PWA = ROOT / "lovceny" / "static" / "pwa.js"

NODE = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');
const sandbox = {globalThis: {}, console};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const {shouldAutoShow, COOLDOWN_MS, ENGAGE_MS} = sandbox.lovcenyPwaPolicy;
const now = 1_700_000_000_000;
const base = {
  storageOk: true, standalone: false, installReady: true,
  sessions: 2, promptedThisSession: false, engaged: true,
  dismissedAt: 0, now,
};
const cases = [
  ['first-session', {...base, sessions: 1}, false],
  ['not-engaged', {...base, engaged: false}, false],
  ['no-install', {...base, installReady: false}, false],
  ['standalone', {...base, standalone: true}, false],
  ['storage-blocked', {...base, storageOk: false}, false],
  ['already-this-session', {...base, promptedThisSession: true}, false],
  ['cooldown', {...base, dismissedAt: now - 1000}, false],
  ['cooldown-expired', {...base, dismissedAt: now - COOLDOWN_MS - 1}, true],
  ['ready', base, true],
];
const out = cases.map(([name, state, expect]) => ({name, ok: shouldAutoShow(state) === expect, got: shouldAutoShow(state)}));
if (ENGAGE_MS !== 20000 || COOLDOWN_MS !== 30 * 864e5) out.push({name: 'constants', ok: false});
console.log(JSON.stringify(out));
if (out.some(x => !x.ok)) process.exit(1);
"""


def test_auto_show_policy():
    node = shutil.which("node")
    assert node
    proc = subprocess.run([node, "-e", NODE, str(PWA)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    rows = json.loads(proc.stdout)
    assert rows and all(r["ok"] for r in rows), rows


def _browser():
    path = ROOT / "tests" / "pwa_prompt_browser.py"
    spec = importlib.util.spec_from_file_location("pwa_prompt_browser", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_server_not_ready_reports_log_instead_of_urlerror(tmp_path):
    """Connection refused nesmí projet jako úspěch ani jako holé URLError."""
    browser = _browser()
    log = tmp_path / "uvicorn.log"
    log.write_text("application startup failed: boom\n")
    proc = subprocess.Popen(["sleep", "30"])
    port = _closed_port()
    try:
        with pytest.raises(SystemExit) as caught:
            browser.wait_http(f"http://127.0.0.1:{port}/healthz", proc, log, "uvicorn", seconds=0.6)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    message = str(caught.value)
    assert "neozval" in message
    assert f"127.0.0.1:{port}/healthz" in message
    assert "application startup failed: boom" in message
    assert message.startswith("uvicorn se do")


def test_exited_server_includes_stderr(tmp_path):
    browser = _browser()
    log = tmp_path / "chrome.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.write('no-usable-sandbox\\n'); raise SystemExit(7)"],
        stdout=fd, stderr=subprocess.STDOUT,
    )
    os.close(fd)
    proc.wait(timeout=5)
    port = _closed_port()
    with pytest.raises(SystemExit) as caught:
        browser.wait_http(f"http://127.0.0.1:{port}/json/version", proc, log, "Chrome", seconds=2)
    message = str(caught.value)
    assert "skončil s kódem 7" in message
    assert "no-usable-sandbox" in message
    assert f"127.0.0.1:{port}/json/version" in message


def test_chrome_binary_is_not_the_shell_wrapper(monkeypatch):
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("Chrome není nainstalovaný")
    monkeypatch.setenv("LOVCENY_CHROME", chrome)
    binary = _browser().chrome_binary()
    assert binary
    with open(binary, "rb") as fh:
        head = fh.read(4)
    assert not head.startswith(b"#!")
    assert head.startswith(b"\x7fELF")


def test_chrome_environment_drops_toolcache_libs():
    env = _browser().chrome_environment({
        "LD_LIBRARY_PATH": "/opt/hostedtoolcache/Python/3.12.16/x64/lib",
        "LD_PRELOAD": "/tmp/libpreload.so",
        "PATH": "/usr/bin",
    })
    assert "LD_LIBRARY_PATH" not in env
    assert "LD_PRELOAD" not in env
    assert env["PATH"] == "/usr/bin"


def test_prompt_behavior_in_browser():
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("Chrome není nainstalovaný")
    script = ROOT / "tests" / "pwa_prompt_browser.py"
    assert script.is_file(), script
    env = os.environ.copy()
    env["LOVCENY_CHROME"] = chrome
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, check=False, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PROMPT_OK" in proc.stdout
