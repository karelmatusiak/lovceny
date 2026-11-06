"""Politika výzvy na plochu: klid, relace, úložiště. Chování v prohlížeči, když je Chrome."""
import json
import shutil
import subprocess
from pathlib import Path

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


def test_prompt_behavior_in_browser():
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        return
    script = ROOT / "tests" / "pwa_prompt_browser.py"
    # The browser check lives next to this test and is invoked below when present.
    if not script.exists():
        return
    proc = subprocess.run(["python3", str(script)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PROMPT_OK" in proc.stdout
