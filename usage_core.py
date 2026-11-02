"""Collect plan usage for Claude Code, Codex, GitHub Copilot and the GLM Coding Plan.

Read-only by design. Every provider is read with credentials that already exist on this machine, and
nothing is ever written back: an expired token is reported, never refreshed (refreshing outside the
owning CLI would rotate its refresh token underneath it). Tokens are only sent to their own provider.

Accounts are discovered automatically; config.json in the app folder can override that (see README).
"""
import base64
import glob
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import threading
import unicodedata
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
IS_MAC = sys.platform == "darwin"
if os.name == "nt":
    APP_DIR = os.path.join(os.environ.get("APPDATA", HOME), "ai-usage-widget")
elif IS_MAC:
    APP_DIR = os.path.join(HOME, "Library", "Application Support", "ai-usage-widget")
else:
    APP_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "ai-usage-widget")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"  # what Claude Code's /usage reads
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"  # what the Codex CLI reads
CODEX_RESETS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"  # GET list only, never redeemed
COPILOT_URL = "https://api.github.com/copilot_internal/user"  # what Copilot clients read
GLM_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"  # GLM Coding Plan quota


def config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat()


def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat() if seconds else None


def _err(e):
    code = getattr(e, "code", None)
    return f"usage unavailable: {'HTTP ' + str(code) if code else type(e).__name__}"


def _get_json(url, headers, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
        return json.load(resp)


# ---------------- Claude Code ----------------

_LAST_GOOD_FILE = os.path.join(APP_DIR, "last-good.json")
_LAST_GOOD_LOCK = threading.Lock()
try:  # last successful figures per account, so a brief rate limit doesn't blank a row
    with open(_LAST_GOOD_FILE, encoding="utf-8") as _fh:
        _LAST_GOOD = json.load(_fh)
except Exception:
    _LAST_GOOD = {}


def _has_claude_login(d):
    """A login lives in .credentials.json (Windows/Linux) or, on macOS, in the Keychain; there the
    folder's .claude.json (or ~/.claude.json for the default folder) records the signed-in account."""
    if os.path.exists(os.path.join(d, ".credentials.json")):
        return True
    if not IS_MAC:
        return False
    meta = os.path.join(HOME, ".claude.json") if _is_default(d) else os.path.join(d, ".claude.json")
    try:
        with open(meta, encoding="utf-8") as fh:
            return bool(json.load(fh).get("oauthAccount"))
    except Exception:
        return False


def _is_default(d):
    return os.path.normcase(os.path.abspath(d)) == os.path.normcase(os.path.join(HOME, ".claude"))


def _keychain_services(d, override=None):
    """macOS Keychain item names Claude Code uses: 'Claude Code-credentials' for the default folder,
    plus '-' + sha256(config dir)[:8] for a CLAUDE_CONFIG_DIR account (Claude Code 2.1.56+)."""
    if override:
        return [override]
    base = "Claude Code-credentials"
    path = os.path.abspath(d)
    hashed = [f"{base}-{hashlib.sha256(unicodedata.normalize('NFC', p).encode()).hexdigest()[:8]}"
              for p in dict.fromkeys((path, path.rstrip("/") + "/", os.path.realpath(path)))]
    return [base] + hashed if _is_default(d) else hashed


def _claude_creds(d, keychain_service=None):
    """claudeAiOauth dict from .credentials.json, else (macOS) from the Keychain. None if not found."""
    cred = os.path.join(d, ".credentials.json")
    if os.path.exists(cred):
        with open(cred, encoding="utf-8") as fh:
            return json.load(fh).get("claudeAiOauth") or {}
    if not IS_MAC:
        return None
    for svc in _keychain_services(d, keychain_service):  # macOS may ask once to allow Keychain access
        r = subprocess.run(["security", "find-generic-password", "-s", svc, "-w"], capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            try:
                return json.loads(r.stdout.strip()).get("claudeAiOauth") or {}
            except ValueError:
                continue
    return None


_KEYCHAIN = {}  # config dir -> keychain_service override from config.json


def claude_accounts():
    """[(name, config_dir)]: config.json "claude" list, else every ~/.claude* folder that holds a login."""
    cfg = config().get("claude")
    if cfg:
        for a in cfg:
            if a.get("keychain_service"):
                _KEYCHAIN[os.path.expanduser(a["dir"])] = a["keychain_service"]
        return [(a["name"], os.path.expanduser(a["dir"])) for a in cfg]
    dirs = [d for d in glob.glob(os.path.join(HOME, ".claude*")) if os.path.isdir(d) and _has_claude_login(d)]
    dirs.sort(key=lambda d: (os.path.basename(d) != ".claude", d))
    return [(os.path.basename(d).lstrip("."), d) for d in dirs]


def claude_usage(net=True):
    accts = claude_accounts()
    if not accts:
        return {"checked": _now_iso(), "rows": []}
    with ThreadPoolExecutor(len(accts)) as ex:
        rows = list(ex.map(lambda a: _claude_row(*a, net), accts))
    return {"checked": _now_iso(), "rows": rows}


def _claude_row(name, d, net):
    default = _is_default(d)
    row = {"name": name, "email": "?", "org": "?", "plan": "", "session": None, "weekly": [], "extra": None,
           "note": "", "login": "logged in", "default": default}
    try:
        meta = os.path.join(HOME, ".claude.json") if default else os.path.join(d, ".claude.json")
        if os.path.exists(meta):
            with open(meta, encoding="utf-8") as fh:
                acct = json.load(fh).get("oauthAccount") or {}
            row["email"] = acct.get("emailAddress", "?")
            org = acct.get("organizationName", "?")
            row["org"] = "Personal" if org.endswith("'s Organization") else org
        o = _claude_creds(d, _KEYCHAIN.get(d))
        if o is None:
            row.update(note="not logged in", login="not logged in")
            return row
        row["plan"] = f"{o.get('subscriptionType', '')} {str(o.get('rateLimitTier', '')).replace('default_claude_', '')}".strip()
        if not net:
            row["note"] = "skipped (offline)"
            return row
        if int(o.get("expiresAt", 0)) < time.time() * 1000:
            row.update(note="idle - token expired (open Claude Code with this account to refresh)", login="expired")
            return row
        hdr = {"Authorization": f"Bearer {o['accessToken']}", "anthropic-beta": "oauth-2025-04-20"}
        for attempt in (0, 1):  # one retry: a burst of checks can get a brief 429
            try:
                u = _get_json(CLAUDE_USAGE_URL, hdr)
                break
            except urllib.error.HTTPError as e:
                if attempt or e.code not in (429, 500, 502, 503, 529):
                    raise
                time.sleep(3)
        for lim in u.get("limits") or []:
            item = {"percent": lim.get("percent"), "resets": lim.get("resets_at"),
                    "label": ((lim.get("scope") or {}).get("model") or {}).get("display_name") or "all"}
            if lim.get("group") == "session" and row["session"] is None:
                row["session"] = item
            elif lim.get("group") == "weekly":
                row["weekly"].append(item)
        ex = u.get("extra_usage") or {}
        row["extra"] = ex.get("utilization") if ex.get("is_enabled") else None
    except Exception as e:  # one bad account never hides the others
        prev = _LAST_GOOD.get(d)
        if prev:
            row.update(session=prev["session"], weekly=prev["weekly"], extra=prev["extra"], stale_since=prev["at"])
        else:
            row["note"] = _err(e)
        return row
    if row["session"] is not None:
        with _LAST_GOOD_LOCK:
            _LAST_GOOD[d] = {"session": row["session"], "weekly": row["weekly"], "extra": row["extra"], "at": _now_iso()}
            try:
                os.makedirs(APP_DIR, exist_ok=True)
                with open(_LAST_GOOD_FILE, "w", encoding="utf-8") as fh:
                    json.dump(_LAST_GOOD, fh)
            except Exception:
                pass
    return row


# ---------------- Codex ----------------

def codex_accounts():
    """[(name, codex_home)]: config.json "codex" list, else ~/.codex plus every ~/.codex-accounts/* seat."""
    cfg = config().get("codex")
    if cfg:
        return [(a["name"], os.path.expanduser(a["dir"])) for a in cfg]
    out = []
    if os.path.exists(os.path.join(HOME, ".codex", "auth.json")):
        out.append(("codex", os.path.join(HOME, ".codex")))
    for d in sorted(glob.glob(os.path.join(HOME, ".codex-accounts", "*"))):
        if os.path.exists(os.path.join(d, "auth.json")):
            out.append((os.path.basename(d), d))
    return out


def _jwt_exp(token):
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp", 0)
    except Exception:
        return 0


def _codex_row(name, d, net):
    row = {"seat": name, "account": "", "plan": "", "state": "fresh", "session": None, "weekly": None,
           "banked": None, "banked_list": None, "note": "", "login": "logged in"}
    try:
        with open(os.path.join(d, "auth.json"), encoding="utf-8") as fh:
            tok = json.load(fh).get("tokens") or {}
        if not tok.get("access_token"):
            row.update(note="not logged in (API key mode?)", login="not logged in", state="idle")
            return row
        if not net:
            row.update(note="skipped (offline)", state="idle")
            return row
        if _jwt_exp(tok["access_token"]) <= time.time():
            row.update(note="idle - token expired (run Codex once to refresh)", login="expired", state="idle")
            return row
        hdr = {"Authorization": f"Bearer {tok['access_token']}", "ChatGPT-Account-Id": tok.get("account_id", ""),
               "User-Agent": "codex-cli"}
        u = _get_json(CODEX_USAGE_URL, hdr)
        rl = u.get("rate_limit") or {}
        p, w = rl.get("primary_window"), rl.get("secondary_window")
        if p:
            row["session"] = {"percent": p.get("used_percent"), "resets": _iso(p.get("reset_at"))}
        if w:
            row["weekly"] = {"percent": w.get("used_percent"), "resets": _iso(w.get("reset_at"))}
        row["banked"] = (u.get("rate_limit_reset_credits") or {}).get("available_count")
        row["account"] = u.get("email") or ""
        row["plan"] = u.get("plan_type") or ""
        row["note"] = "limit reached" if rl.get("limit_reached") else ""
        try:  # banked resets with their expiry dates
            creds = _get_json(CODEX_RESETS_URL, hdr).get("credits") or []
            row["banked_list"] = sorted(({"title": c.get("title"), "expires": c.get("expires_at")} for c in creds
                                         if c.get("status") == "available"), key=lambda c: c["expires"] or "")
        except Exception:
            pass
    except Exception as e:
        row.update(note=_err(e), state="idle")
    return row


def codex_usage(net=True):
    accts = codex_accounts()
    if not accts:
        return {"checked": _now_iso(), "rows": []}
    with ThreadPoolExecutor(len(accts)) as ex:
        rows = list(ex.map(lambda a: _codex_row(*a, net), accts))
    return {"checked": _now_iso(), "rows": rows}


# ---------------- GitHub Copilot ----------------

def copilot_logins():
    """config.json "copilot" list of GitHub logins, else the accounts logged into the Copilot CLI."""
    cfg = config().get("copilot")
    if cfg is not None:
        return list(cfg)
    try:
        with open(os.path.join(HOME, ".copilot", "config.json"), encoding="utf-8") as fh:
            j = json.loads(re.sub(r"(?m)^\s*//.*$", "", fh.read()))
        return sorted({u.get("login") for u in j.get("loggedInUsers") or [] if u.get("login")})
    except Exception:
        return []


def _copilot_row(login, net):
    row = {"login": login, "plan": "", "premium": None, "unlimited": False, "resets": None, "note": "", "status": "logged in"}
    if not net:
        row["note"] = "skipped (offline)"
        return row
    try:  # token from the GitHub CLI's existing login for this user
        tok = subprocess.run(["gh", "auth", "token", "--user", login], capture_output=True, text=True, timeout=15,
                             creationflags=NO_WINDOW).stdout.strip()
        if not tok:
            row.update(note="no gh login for this user", status="not logged in")
            return row
        d = _get_json(COPILOT_URL, {"Authorization": f"token {tok}", "Accept": "application/json",
                                    "User-Agent": "GitHubCopilotChat/0.26.7", "Editor-Version": "vscode/1.99.0"})
        q = (d.get("quota_snapshots") or {}).get("premium_interactions") or {}
        row["plan"] = d.get("copilot_plan") or ""
        row["unlimited"] = bool(q.get("unlimited"))
        if not row["unlimited"] and q.get("percent_remaining") is not None:
            row["premium"] = {"percent": round(100 - q["percent_remaining"]), "remaining": q.get("remaining"),
                              "entitlement": q.get("entitlement")}
        row["resets"] = d.get("quota_reset_date_utc") or d.get("quota_reset_date")
    except Exception as e:
        row["note"] = _err(e)
    return row


def copilot_usage(net=True):
    logins = copilot_logins()
    if not logins:
        return {"checked": _now_iso(), "rows": []}
    with ThreadPoolExecutor(len(logins)) as ex:
        rows = list(ex.map(lambda l: _copilot_row(l, net), logins))
    return {"checked": _now_iso(), "rows": rows}


# ---------------- GLM Coding Plan (z.ai) ----------------

def _glm_key():
    k = os.environ.get("ZAI_API_KEY") or os.environ.get("GLM_API_KEY")
    path = os.path.join(APP_DIR, "zai.key")
    if not k and os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            k = fh.read().strip()
    return k or None


def glm_usage(net=True):
    key = _glm_key()
    if not key:
        return {"checked": _now_iso(), "rows": []}
    row = {"name": "GLM Coding Plan", "plan": "", "limits": [], "note": "", "status": "logged in"}
    if not net:
        row["note"] = "skipped (offline)"
        return {"checked": _now_iso(), "rows": [row]}
    try:
        u = None
        for auth in (f"Bearer {key}", key):  # accept either header form
            try:
                u = _get_json(GLM_QUOTA_URL, {"Authorization": auth, "Accept": "application/json"})
                break
            except urllib.error.HTTPError as e:
                if e.code not in (401, 403) or auth == key:
                    raise
        data = (u or {}).get("data") or u or {}
        row["plan"] = data.get("level") or data.get("planName") or ""
        for lim in data.get("limits") or []:
            pct, reset = lim.get("percentage"), lim.get("nextResetTime")
            if isinstance(reset, (int, float)):
                reset = _iso(reset / (1000 if reset > 1e11 else 1))
            row["limits"].append({"type": lim.get("type") or "", "unit": lim.get("unit"), "number": lim.get("number"),
                                  "percent": round(pct) if isinstance(pct, (int, float)) else None, "resets": reset,
                                  "used": lim.get("currentValue"), "total": lim.get("usage")})
        if not row["limits"]:
            row["note"] = "no quota data returned"
    except Exception as e:
        code = getattr(e, "code", None)
        row["note"] = "key rejected" if code in (401, 403) else _err(e)
        row["status"] = "expired" if code in (401, 403) else "logged in"
    return {"checked": _now_iso(), "rows": [row]}


# ---------------- all providers ----------------

def collect(net=True):
    """Every provider at once; accounts within a provider in parallel too."""
    with ThreadPoolExecutor(4) as ex:
        futs = {k: ex.submit(f, net) for k, f in (("claude", claude_usage), ("codex", codex_usage),
                                                    ("copilot", copilot_usage), ("glm", glm_usage))}
        out = {k: f.result() for k, f in futs.items()}
    out["checked"] = _now_iso()
    return out


def demo():
    """Made-up accounts for screenshots and recordings. No files read, no network."""
    now = datetime.now(timezone.utc)
    at = lambda **kw: (now + timedelta(**kw)).isoformat()
    jitter = random.Random(int(time.time() // 120))  # changes every 2 min so a recording shows movement
    pct = lambda base: max(0, min(100, base + jitter.randint(-3, 3)))
    claude = [
        ("work", "alice@example.com", "team max_5x", pct(64), [("Opus", pct(41))], True),
        ("personal", "alice.dev@example.org", "pro", pct(22), [("all", pct(58))], False),
        ("client-a", "alice@client-a.example", "team max_5x", pct(91), [("Opus", pct(77))], False),
        ("side-project", "alice@side.example", "max", 0, [("all", pct(12))], False),
    ]
    rows = [{"name": n, "email": e, "org": "Example Org", "plan": p, "default": dflt, "note": "", "login": "logged in",
             "session": {"percent": s, "resets": at(hours=2, minutes=14)},
             "weekly": [{"label": lbl, "percent": w, "resets": at(days=3, hours=5)} for lbl, w in wk], "extra": None}
            for n, e, p, s, wk, dflt in claude]
    rows.append({"name": "archive", "email": "old@example.net", "org": "Example Org", "plan": "pro", "default": False,
                 "note": "idle - token expired", "login": "expired", "session": None, "weekly": [], "extra": None})
    codex = []
    for i, (s, w, b) in enumerate([(12, 34, 3), (88, 61, 2), (97, 72, 3), (45, 20, 1), (0, 8, 3), (63, 49, 0)], 1):
        codex.append({"seat": f"seat{i:02d}", "account": f"dev{i}@example.com", "plan": "team", "state": "fresh",
                      "session": {"percent": pct(s) if s else 0, "resets": at(hours=1 + i % 4, minutes=7 * i)},
                      "weekly": {"percent": pct(w), "resets": at(days=1 + i % 5, hours=i)}, "banked": b,
                      "banked_list": [{"title": "Full reset (Weekly + 5 hr)", "expires": at(days=5 + 9 * k, hours=k)}
                                      for k in range(b)], "note": "", "login": "logged in"})
    return {"checked": _now_iso(),
            "claude": {"checked": _now_iso(), "rows": rows},
            "codex": {"checked": _now_iso(), "rows": codex},
            "copilot": {"checked": _now_iso(), "rows": [
                {"login": "octo-alice", "plan": "enterprise", "unlimited": True, "premium": None,
                 "resets": at(days=26), "note": "", "status": "logged in"},
                {"login": "alice-oss", "plan": "pro", "unlimited": False, "resets": at(days=26), "note": "",
                 "premium": {"percent": pct(37), "remaining": 189, "entitlement": 300}, "status": "logged in"}]},
            "glm": {"checked": _now_iso(), "rows": [
                {"name": "GLM Coding Plan", "plan": "pro", "note": "", "status": "logged in", "limits": [
                    {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percent": pct(28), "resets": at(hours=3),
                     "used": 560, "total": 2000},
                    {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "percent": pct(15), "resets": at(days=4),
                     "used": 1500, "total": 10000}]}]}}


if __name__ == "__main__":
    print(json.dumps(demo() if "--demo" in os.sys.argv else collect(), indent=1))
