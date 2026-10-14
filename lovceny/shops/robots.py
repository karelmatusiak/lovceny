"""robots.txt parser with Google-style wildcards (`*`, `$`) and longest-match precedence.
The stdlib urllib.robotparser ignores wildcards (e.g. it would allow xzone.cz `/*?*term=`)."""
import re
from urllib.parse import urlparse


def _rx(path: str):
    end = path.endswith("$")
    p = re.escape(path[:-1] if end else path).replace(r"\*", ".*")
    return re.compile(p + ("$" if end else ""))


class Robots:
    def __init__(self, text: str = ""):
        self.groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        agents, rules, last_was_rule = [], [], False
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            k, v = (x.strip() for x in line.split(":", 1))
            k = k.lower()
            if k == "user-agent":
                if last_was_rule:
                    self.groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(v.lower())
                last_was_rule = False
            elif k in ("allow", "disallow") and agents:
                last_was_rule = True
                if v:
                    rules.append((k == "allow", v))
        if agents:
            self.groups.append((agents, rules))

    def _rules_for(self, agent: str):
        a = agent.lower()
        specific = [r for ags, r in self.groups if any(x != "*" and x in a for x in ags)]
        if specific:
            return [x for r in specific for x in r]
        return [x for ags, r in self.groups if "*" in ags for x in r]

    def can_fetch(self, agent: str, url: str) -> bool:
        p = urlparse(url)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        best = None  # (length, allow)
        for allow, rule in self._rules_for(agent):
            if _rx(rule).match(path):
                cand = (len(rule), allow)
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and allow):
                    best = cand
        return True if best is None else best[1]
