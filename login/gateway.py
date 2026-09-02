#!/usr/bin/env python3
"""
NetMirror access gateway (auth + reverse proxy), stdlib only.

Single process that:
  * listens on public :3000
  * serves a custom login page and validates multi-account credentials
  * sets an HttpOnly session cookie
  * reverse-proxies every other request to the panel/agent on 127.0.0.1:3001
  * leaves /api/ and /session open (they are already gated by the panel's
    own ADMIN_API_KEY; the browser UI supplies it)

Two modes:
  PANEL (default): unauthenticated UI requests -> login page.
  AGENT (AGENT_MODE=true or ALLOW_IPS set): no UI; proxies everything upstream.
    - ALLOW_IPS empty -> public agent (needed when the panel UI calls the agent
      directly from the browser).
    - ALLOW_IPS set  -> only those source IPs may pass.

Performance notes (white-screen fix):
  * Static text assets (html/js/css) are gzip-compressed on the fly when the
    client accepts gzip. This cuts the ~2 MB SPA bundle to ~25% on the wire.
  * Content-addressed assets (/js/*-*.js, /css/*-*.css) get
    `Cache-Control: immutable` so the browser fetches them ONCE; repeat visits
    need only index.html + the SSE handshake -> no more multi-second white screen.
  * A pure-CSS loading spinner is injected into #app so the user sees feedback
    instantly (before Vue even boots) instead of a blank white page.

Run with --network host (or natively) so 127.0.0.1:3001 is reachable.
"""
import http.server
import socketserver
import urllib.parse
import hashlib
import hmac
import secrets
import json
import os
import sys
import time
import socket
import gzip
import re
import threading
import traceback
import select
import base64

PORT = int(os.environ.get("PORT", "3000"))
UPSTREAM_HOST, UPSTREAM_PORT = os.environ.get("UPSTREAM", "127.0.0.1:3001").split(":")
UPSTREAM_PORT = int(UPSTREAM_PORT)
USERS_FILE = os.environ.get("USERS_FILE", "/data/users.txt")
SESS_FILE = os.environ.get("SESS_FILE", "/data/sessions.json")
SESSION_TTL = int(os.environ.get("SESSION_TTL", "43200"))  # 12h
COOKIE_NAME = "nm_sess"
# agent mode: no login page; proxies everything to the upstream agent.
# AGENT_MODE can be explicit (env=1/true/yes) or implicit (ALLOW_IPS set).
AGENT_MODE = os.environ.get("AGENT_MODE", "").lower() in ("1", "true", "yes") or \
    bool(os.environ.get("ALLOW_IPS", "").strip())
# comma-separated allowed source IPs for agent mode; empty = public (no restriction)
ALLOW_IPS = [x.strip() for x in os.environ.get("ALLOW_IPS", "").split(",") if x.strip()]
# panel mode: trusted peer panel IPs that may call this panel server-to-server
# without a login cookie (preserves dual-panel mutual management).
PEER_IPS = [x.strip() for x in os.environ.get("PEER_IPS", "").split(",") if x.strip()]

SESSIONS = {}  # token -> {"user":..., "exp":...}

# --- tool access control (per-gateway, file-driven, hot-reloaded) ---
# tools.json lives in the data dir. Tools not listed default to ENABLED.
# Disabling a tool (a) blocks the request at the gateway (403) and
# (b) rewrites the SSE `Config` feature flags so the panel UI hides it.
TOOLS_FILE = os.environ.get("TOOLS_FILE") or ""

# canonical tool ids
TOOL_IDS = [
    "ping", "ping6", "mtr", "mtr6", "traceroute", "traceroute6",
    "iperf3", "speedtest_dot_net", "shell", "librespeed", "filespeedtest",
]
TOOL_LABELS = {
    "ping": "Ping (IPv4)",
    "ping6": "Ping (IPv6)",
    "mtr": "MTR (IPv4)",
    "mtr6": "MTR (IPv6)",
    "traceroute": "Traceroute (IPv4)",
    "traceroute6": "Traceroute (IPv6)",
    "iperf3": "iPerf3",
    "speedtest_dot_net": "Speedtest.net (CLI)",
    "shell": "交互式 Shell",
    "librespeed": "LibreSpeed 测速",
    "filespeedtest": "文件下载测速",
}
# logical UI-level toggles (IPv4/IPv6 share one feature flag)
TOOL_BASE_IDS = [
    "ping", "mtr", "traceroute", "iperf3",
    "speedtest_dot_net", "shell", "librespeed", "filespeedtest",
]
TOOL_UI_LABELS = {
    "ping": "Ping (IPv4/IPv6)",
    "mtr": "MTR (IPv4/IPv6)",
    "traceroute": "Traceroute (IPv4/IPv6)",
    "iperf3": "iPerf3",
    "speedtest_dot_net": "Speedtest.net (CLI)",
    "shell": "交互式 Shell",
    "librespeed": "LibreSpeed 测速",
    "filespeedtest": "文件下载测速",
}
# map tool id -> panel Config feature flag (IPv4/IPv6 share the same flag)
TOOL_FEATURE = {
    "ping": "feature_ping",
    "ping6": "feature_ping",
    "mtr": "feature_mtr",
    "mtr6": "feature_mtr",
    "traceroute": "feature_traceroute",
    "traceroute6": "feature_traceroute",
    "iperf3": "feature_iperf3",
    "speedtest_dot_net": "feature_speedtest_dot_net",
    "shell": "feature_shell",
    "librespeed": "feature_librespeed",
    "filespeedtest": "feature_filespeedtest",
}

_tools_cache = {"mtime": -1, "data": None}


def load_tools():
    """Return the {tool_id: bool} map from tools.json, hot-reloaded on change."""
    global _tools_cache
    tf = _tools_file()
    try:
        mtime = os.path.getmtime(tf)
    except OSError:
        mtime = -1
    if _tools_cache["data"] is not None and _tools_cache["mtime"] == mtime:
        return _tools_cache["data"]
    data = {}
    if os.path.exists(tf):
        try:
            with open(tf) as f:
                data = json.load(f).get("tools", {})
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    _tools_cache = {"mtime": mtime, "data": data}
    return data


def tool_enabled(tid):
    """A tool is enabled unless tools.json explicitly sets it false.
    IPv6 variants (ping6/mtr6/traceroute6) share the IPv4 feature flag."""
    base = tid[:-1] if tid.endswith("6") else tid
    return bool(load_tools().get(base, True))


def _tools_file():
    """Resolve the tools.json path (env override > data dir)."""
    return TOOLS_FILE or os.path.join(_data_dir(), "tools.json")


def tool_for_path(path):
    """Map a request path to a tool id, or None if it is not a tool endpoint."""
    base = path.split("?")[0]
    exact = {
        "/method/ping6": "ping6",
        "/method/ping": "ping",
        "/method/mtr6": "mtr6",
        "/method/mtr": "mtr",
        "/method/traceroute6": "traceroute6",
        "/method/traceroute": "traceroute",
        "/method/iperf3/server": "iperf3",
        "/method/speedtest_dot_net": "speedtest_dot_net",
    }
    if base in exact:
        return exact[base]
    if base.startswith("/session/") and base.endswith("/shell"):
        return "shell"
    if base.startswith("/session/") and "/speedtest/download" in base:
        return "librespeed"
    if base.startswith("/session/") and "/speedtest/file/" in base:
        return "filespeedtest"
    return None


# --- compression / caching knobs ---
MAX_COMPRESS = 25 * 1024 * 1024  # never buffer/compress bodies larger than 25 MB
COMPRESSIBLE = ("text/html", "text/javascript", "application/javascript", "text/css")
# hashed, content-addressed asset paths -> safe to cache forever
HASHED_ASSET_RE = re.compile(r"/(?:js|css)/[^/]+-[A-Za-z0-9_-]+\.(?:js|css)$")


# --------------------------------------------------------------------------
def load_users():
    users = {}
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    u, salt, h = line.split(":", 2)
                    users[u] = (salt, h)
                except ValueError:
                    continue
    return users


def verify(user, pw):
    users = load_users()
    if user not in users:
        return False
    salt, h = users[user]
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 100000).hex()
    return hmac.compare_digest(calc, h)


def load_sessions():
    global SESSIONS
    if os.path.exists(SESS_FILE):
        try:
            with open(SESS_FILE) as f:
                SESSIONS = json.load(f)
        except Exception:
            SESSIONS = {}
    now = time.time()
    SESSIONS = {k: v for k, v in SESSIONS.items() if v.get("exp", 0) > now}


def save_sessions():
    try:
        with open(SESS_FILE, "w") as f:
            json.dump(SESSIONS, f)
    except Exception:
        pass


def new_session(user):
    tok = secrets.token_hex(32)
    SESSIONS[tok] = {"user": user, "exp": time.time() + SESSION_TTL}
    save_sessions()
    return tok


def drop_session(tok):
    SESSIONS.pop(tok, None)
    save_sessions()


def session_user(token):
    s = SESSIONS.get(token)
    if not s:
        return None
    if s.get("exp", 0) < time.time():
        drop_session(token)
        return None
    return s["user"]


# --------------------------------------------------------------------------
# Share links: password-protected, time-limited access for external testers.
# Stored server-side in shares.json ({ id: {salt, phash, exp, created, note} }).
# The gateway reloads the file on mtime change, so the management tool can add
# / revoke shares without restarting the gateway. Expiry is enforced on EVERY
# proxied request, so an expired link stops working immediately.
SHARE_COOKIE = "nm_share"
SHARE_PW_ITER = 100000
SHARES = {}
SHARES_MTIME = 0


def _data_dir():
    for d in ("/opt/nm-gateway", "/data"):
        if os.path.isdir(d):
            return d
    return "/data"


SHARES_FILE = os.environ.get("SHARES_FILE", os.path.join(_data_dir(), "shares.json"))


def load_shares(force=False):
    global SHARES, SHARES_MTIME
    try:
        m = os.path.getmtime(SHARES_FILE)
    except OSError:
        if force:
            SHARES = {}
        return
    if not force and m == SHARES_MTIME:
        return
    try:
        with open(SHARES_FILE) as f:
            SHARES = json.load(f)
        SHARES_MTIME = m
    except Exception:
        if force:
            SHARES = {}


def _share_valid(share_id):
    load_shares()
    s = SHARES.get(share_id)
    if not s:
        return False
    if s.get("exp", 0) < time.time():
        return False
    return True


def _verify_share_pw(share_id, pw):
    load_shares()
    s = SHARES.get(share_id)
    if not s:
        return False
    if s.get("exp", 0) < time.time():
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(s["salt"]), SHARE_PW_ITER).hex()
    return hmac.compare_digest(calc, s["phash"])


# --------------------------------------------------------------------------
# Admin console: key-gated web UI for managing share links + nodes.
# The console entry password lives in ADMIN_KEY_FILE (or CONSOLE_KEY env); the
# panel admin API key (for node CRUD) lives in PANEL_KEY_FILE / PANEL_API_KEY.
# They are deliberately decoupled: the operator can set an easy console password
# without it needing to match the panel's API key used to drive /api/admin/*.
ADMIN_COOKIE = "nm_admin"
ADMIN_KEY_FILE = os.environ.get("ADMIN_KEY_FILE", os.path.join(_data_dir(), "admin.key"))
PANEL_KEY_FILE = os.environ.get("PANEL_KEY_FILE", os.path.join(_data_dir(), "panel.key"))
PANEL_API_KEY = os.environ.get("PANEL_API_KEY", "")
ADMIN_TTL = int(os.environ.get("ADMIN_TTL", "43200"))  # 12h
ADMIN_SESS_FILE = os.path.join(_data_dir(), "admin_sessions.json")
ADMIN_SESSIONS = {}  # token -> {"exp":...}

_console_key_cache = {"val": None, "mtime": 0}
_panel_key_cache = {"val": None, "mtime": 0}


def _console_key():
    """Console entry password (env CONSOLE_KEY > admin.key file)."""
    env = os.environ.get("CONSOLE_KEY", "")
    if env:
        return env
    global _console_key_cache
    try:
        m = os.path.getmtime(ADMIN_KEY_FILE)
    except OSError:
        return ""
    if m == _console_key_cache["mtime"] and _console_key_cache["val"] is not None:
        return _console_key_cache["val"]
    try:
        with open(ADMIN_KEY_FILE) as f:
            v = f.read().strip()
        _console_key_cache["val"] = v
        _console_key_cache["mtime"] = m
        return v
    except Exception:
        return ""


def _panel_key():
    """Panel ADMIN_API_KEY used to drive /api/admin/* (env > panel.key file)."""
    if PANEL_API_KEY:
        return PANEL_API_KEY
    global _panel_key_cache
    try:
        m = os.path.getmtime(PANEL_KEY_FILE)
    except OSError:
        return ""
    if m == _panel_key_cache["mtime"] and _panel_key_cache["val"] is not None:
        return _panel_key_cache["val"]
    try:
        with open(PANEL_KEY_FILE) as f:
            v = f.read().strip()
        _panel_key_cache["val"] = v
        _panel_key_cache["mtime"] = m
        return v
    except Exception:
        return ""


def load_admin_sessions():
    global ADMIN_SESSIONS
    if os.path.exists(ADMIN_SESS_FILE):
        try:
            with open(ADMIN_SESS_FILE) as f:
                ADMIN_SESSIONS = json.load(f)
        except Exception:
            ADMIN_SESSIONS = {}
    now = time.time()
    ADMIN_SESSIONS = {k: v for k, v in ADMIN_SESSIONS.items() if v.get("exp", 0) > now}


def save_admin_sessions():
    try:
        with open(ADMIN_SESS_FILE, "w") as f:
            json.dump(ADMIN_SESSIONS, f)
    except Exception:
        pass


# --------------------------------------------------------------------------
# ---- light/dark theme toggle, available BEFORE login, persisted to the
#      same `theme` key used by the NetMirror SPA (localStorage) ----
THEME_MODE_HEAD = (
    '<style>'
    ':root{'
    '--bg:#0b1020;--bg2:#16203a;--card:#141b2e;--fg:#e6ebf5;--muted:#8b97b3;'
    '--accent:#4f8cff;--ok:#3ddc97;--err:#ff6b6b;'
    '--border:#23304d;--input-bg:#0e1424;--code-bg:#0e1424;'
    '--ghost-bg:#1d2942;--danger-bg:#3a2030;--danger-fg:#ff9b9b;'
    '--badge-ok-bg:#10331f;--badge-exp-bg:#3a2030;--shadow:rgba(0,0,0,.45);'
    '}'
    ':root[data-theme="light"]{'
    '--bg:#f3f6ff;--bg2:#e7edff;--card:#ffffff;--fg:#111827;--muted:#5a6b8c;'
    '--accent:#4f8cff;--ok:#059669;--err:#dc2626;'
    '--border:#d1d9f0;--input-bg:#e7edff;--code-bg:#e7edff;'
    '--ghost-bg:#e2e8f0;--danger-bg:#fee2e2;--danger-fg:#991b1b;'
    '--badge-ok-bg:#d1fae5;--badge-exp-bg:#fee2e2;--shadow:rgba(0,0,0,.1);'
    '}'
    'body{background:radial-gradient(1200px 600px at 50% -10%,var(--bg2),var(--bg))}'
    '.theme-toggle{position:fixed;bottom:18px;right:18px;z-index:99999;'
    'display:inline-flex;align-items:center;justify-content:center;'
    'width:40px;height:40px;border-radius:50%;border:1px solid var(--border);'
    'background:var(--card);color:var(--fg);cursor:pointer;box-shadow:0 4px 12px var(--shadow);'
    'font-size:18px;transition:transform .12s}'
    '.theme-toggle:hover{transform:scale(1.08)}'
    '</style>'
    '<script>(function(){'
    'var t=localStorage.getItem("theme");'
    'if(!t){try{var m=matchMedia("(prefers-color-scheme: light)");if(m&&m.matches)t="light";}catch(e){}}'
    'if(!t)t="dark";'
    't=t.toLowerCase();'
    'if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);'
    '})();</script>'
)
THEME_MODE_BODY = (
    '<button class="theme-toggle" id="nm-theme-toggle" type="button" title="切换主题">'
    '<span id="nm-theme-icon">🌙</span>'
    '</button>'
    '<script>(function(){'
    'var b=document.getElementById("nm-theme-toggle"),i=document.getElementById("nm-theme-icon");'
    'function setTheme(t){document.documentElement.setAttribute("data-theme",t);try{localStorage.setItem("theme",t);}catch(e){}}'
    'function get(){var t=document.documentElement.getAttribute("data-theme");if(t)return t;try{var v=localStorage.getItem("theme");if(v)return v.toLowerCase();}catch(e){}return "dark";}'
    'function sync(){var t=get();i.textContent=t==="light"?"☀️":"🌙";b.title=t==="light"?"切换到深色":"切换到浅色";}'
    'if(b)b.onclick=function(){setTheme(get()==="light"?"dark":"light");sync();};'
    'sync();'
    '})();</script>'
)

# ---- floating top-right bar: console + logout ----
GW_BAR_SNIPPET = (
    '<style>'
    '#nm-gw-bar{position:fixed;top:14px;right:16px;z-index:2147483647;'
    'display:flex;align-items:center;gap:8px}'
    '.nm-gw-btn{display:inline-flex;align-items:center;gap:6px;padding:8px 14px;'
    'background:rgba(20,27,46,.92);color:#e6ebf5;border:1px solid #2a3654;'
    'border-radius:10px;font:600 13px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;'
    'text-decoration:none;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.35)}'
    '.nm-gw-btn:hover{background:var(--accent,#4f8cff);color:#fff}'
    'html:not(.dark) .nm-gw-btn{background:rgba(255,255,255,.92);color:#111827;border-color:#d1d9f0}'
    '</style>'
    '<div id="nm-gw-bar">'
    '<a class="nm-gw-btn" href="/console" title="管理控制台">控制台</a>'
    '<a class="nm-gw-btn" href="/logout" title="退出登录">退出登录</a>'
    '</div>'
)

LOGIN_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NetMirror 登录</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg)}
  .card{width:340px;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px 26px;
        box-shadow:0 20px 60px var(--shadow)}
  .logo{display:flex;align-items:center;gap:10px;margin-bottom:18px}
  .dot{width:12px;height:12px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px var(--accent)}
  h1{font-size:18px;margin:0;font-weight:600}
  .sub{color:var(--muted);font-size:13px;margin:6px 0 20px}
  label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px}
  input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--border);background:var(--input-bg);color:var(--fg);font-size:14px;outline:none}
  input:focus{border-color:var(--accent)}
  button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:9px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
  button:hover{filter:brightness(1.08)}
  .err{color:var(--err);font-size:13px;min-height:18px;margin-top:12px}
</style>
</head>
<body>
  <form class="card" method="post" action="/login">
    <div class="logo"><span class="dot"></span><h1>NetMirror</h1></div>
    <div class="sub">请输入账号与密码以访问控制台</div>
    <label for="u">账号</label>
    <input id="u" name="user" autocomplete="username" placeholder="username" required autofocus>
    <label for="p">密码</label>
    <input id="p" name="pass" type="password" autocomplete="current-password" placeholder="password" required>
    <button type="submit">登 录</button>
    <div class="err">{msg}</div>
  </form>
</body>
</html>
"""


SHARE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>测试链接访问</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg)}
  .card{width:360px;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px 26px;
        box-shadow:0 20px 60px var(--shadow)}
  .logo{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .dot{width:12px;height:12px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px var(--accent)}
  h1{font-size:18px;margin:0;font-weight:600}
  .sub{color:var(--muted);font-size:13px;margin:6px 0 18px}
  label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px}
  input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--border);background:var(--input-bg);color:var(--fg);font-size:14px;outline:none}
  input:focus{border-color:var(--accent)}
  button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:9px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
  button:hover{filter:brightness(1.08)}
  .err{color:var(--err);font-size:13px;min-height:18px;margin-top:12px}
</style>
</head>
<body>
  <form class="card" method="post" action="/share">
    <div class="logo"><span class="dot"></span><h1>NetMirror 测试</h1></div>
    <div class="sub">请输入访问密码以进入测试控制台</div>
    <input type="hidden" name="id" value="{id}">
    <label for="p">访问密码</label>
    <input id="p" name="pass" type="password" autocomplete="current-password" placeholder="password" required autofocus>
    <button type="submit">进 入</button>
    <div class="err">{msg}</div>
  </form>
</body>
</html>
"""

EXPIRED_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>链接已过期</title><style>
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(1200px 600px at 50% -10%,#16203a,#0b1020);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;color:#e6ebf5}
.card{text-align:center;padding:40px}
.dot{width:14px;height:14px;border-radius:50%;background:#ff6b6b;display:inline-block;margin-bottom:16px}
h1{font-size:20px;margin:0 0 8px}
p{color:#8b97b3;font-size:14px}
</style></head><body><div class="card"><span class="dot"></span><h1>测试链接已过期</h1><p>该分享链接已超过有效时间，请联系管理员获取新的链接。</p></div></body></html>
"""


# Key-entry gate for the admin console (replaced: {msg}).
CONSOLE_LOGIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>控制台登录</title>
<style>
*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg)}
.card{width:360px;background:var(--card);border:1px solid var(--border);border-radius:14px;padding:28px 26px;box-shadow:0 20px 60px var(--shadow)}
.logo{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.dot{width:12px;height:12px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px var(--accent)}
h1{font-size:18px;margin:0;font-weight:600}
.sub{color:var(--muted);font-size:13px;margin:6px 0 20px}
label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px}
input{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--border);background:var(--input-bg);color:var(--fg);font-size:14px;outline:none}
input:focus{border-color:var(--accent)}
button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:9px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.08)}
.err{color:var(--err);font-size:13px;min-height:18px;margin-top:12px}
</style>
</head>
<body>
<form class="card" method="post" action="/console">
<input type="hidden" name="action" value="login">
<div class="logo"><span class="dot"></span><h1>节点管理控制台</h1></div>
<div class="sub">请输入管理密钥以进入控制台</div>
<label for="k">管理密钥</label>
<input id="k" name="key" type="password" placeholder="admin key" required autofocus>
<button type="submit">进 入</button>
<div class="err">{msg}</div>
</form>
</body>
</html>
"""


# The console SPA. Pure vanilla JS; talks to /console/api/* (same-origin).
CONSOLE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>节点管理控制台</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(1200px 600px at 50% -10%,var(--bg2),var(--bg));color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;padding:24px}
.wrap{max-width:980px;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
h1{font-size:20px;margin:0}
.tag{font-size:12px;color:var(--muted)}
.logout{color:var(--muted);text-decoration:none;font-size:13px;border:1px solid var(--border);padding:6px 12px;border-radius:8px}
.logout:hover{color:var(--fg)}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:20px}
.card h2{font-size:15px;margin:0 0 14px;color:var(--fg)}
.row{display:flex;gap:12px;flex-wrap:wrap}
.field{display:flex;flex-direction:column;gap:6px;flex:1;min-width:160px}
label{font-size:12px;color:var(--muted)}
input{width:100%;padding:10px 12px;border-radius:9px;border:1px solid var(--border);background:var(--input-bg);color:var(--fg);font-size:14px;outline:none}
input:focus{border-color:var(--accent)}
button{margin-top:14px;padding:10px 18px;border:0;border-radius:9px;background:var(--accent);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.08)}
button.ghost{background:var(--ghost-bg);color:var(--fg)}
button.danger{background:var(--danger-bg);color:var(--danger-fg)}
.msg{font-size:13px;margin-top:10px;min-height:18px}
.msg.err{color:var(--err)}
.msg.ok{color:var(--ok)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--border)}
th{color:var(--muted);font-weight:600}
.urlbox{display:flex;gap:8px;align-items:center;margin-top:12px}
.urlbox input{font-family:monospace;font-size:12px}
.badge{font-size:11px;padding:2px 8px;border-radius:999px}
.badge.ok{background:var(--badge-ok-bg);color:var(--ok)}
.badge.exp{background:var(--badge-exp-bg);color:var(--err)}
code{background:var(--code-bg);padding:2px 6px;border-radius:6px;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
<header>
<div><h1>节点管理控制台</h1><div class="tag">NetMirror · 分享链接与节点管理</div></div>
<a class="logout" href="/console/logout">退出登录</a>
</header>

<div class="card">
<h2>生成测试分享链接</h2>
<div class="row">
<div class="field"><label>访问密码</label><input id="pw" type="password" placeholder="tester 访问密码"></div>
<div class="field"><label>有效时长（分钟）</label><input id="min" type="number" value="60" min="1" max="43200"></div>
<div class="field"><label>备注</label><input id="note" placeholder="例如：客户A测速"></div>
</div>
<button onclick="createShare()">生成链接</button>
<div class="msg" id="shareMsg"></div>
<div class="urlbox" id="shareResult" style="display:none">
<input id="shareUrl" readonly><button class="ghost" onclick="copyUrl()">复制</button>
</div>
</div>

<div class="card">
<h2>已有分享链接</h2>
<table id="shareTable"><thead><tr><th>ID</th><th>备注</th><th>过期时间</th><th>状态</th><th>操作</th></tr></thead><tbody></tbody></table>
</div>

<div class="card">
<h2>节点列表</h2>
<table id="nodeTable"><thead><tr><th>名称</th><th>位置</th><th>URL</th><th>当前</th><th>操作</th></tr></thead><tbody></tbody></table>
<div class="row" style="margin-top:16px">
<div class="field"><label>名称</label><input id="nName" placeholder="HKG1-Node3"></div>
<div class="field"><label>位置</label><input id="nLoc" placeholder="Hong Kong"></div>
<div class="field"><label>URL</label><input id="nUrl" placeholder="http://ip:3000"></div>
</div>
<button onclick="addNode()">添加节点</button>
<div class="msg" id="nodeMsg"></div>
</div>

<div class="card">
<h2>修改控制台密码</h2>
<div class="row">
<div class="field"><label>当前密码</label><input id="oldPw" type="password"></div>
<div class="field"><label>新密码</label><input id="newPw" type="password"></div>
</div>
<button onclick="setPw()">保存新密码</button>
<div class="msg" id="pwMsg"></div>
</div>
</div>

<div class="card">
<h2>Network Tools 开放控制</h2>
<p style="color:var(--muted);font-size:13px;margin:0 0 14px">关闭某项后，网关会拒绝该工具的请求（403），并在面板 UI 中隐藏对应按钮。</p>
<div id="toolsList" style="display:flex;flex-direction:column;gap:10px"></div>
<button onclick="saveTools()">保存设置</button>
<div class="msg" id="toolsMsg"></div>
</div>

<script>
const api=(path,opts={})=>fetch(path,Object.assign({credentials:'same-origin'},opts));
function fmt(ts){ if(!ts) return '-'; const d=new Date(ts*1000); return d.toLocaleString(); }
async function loadShares(){
  try{ const r=await api('/console/api/shares'); const d=await r.json(); renderShares(d.shares||[]); }
  catch(e){ console.error(e); }
}
function renderShares(list){
  const tb=document.querySelector('#shareTable tbody'); tb.innerHTML='';
  list.forEach(s=>{
    const tr=document.createElement('tr');
    const exp=s.expired?'<span class="badge exp">已过期</span>':'<span class="badge ok">有效</span>';
    const url=location.origin+'/share?id='+s.id;
    const td1=document.createElement('td'); td1.innerHTML='<code>'+s.id+'</code>';
    const td2=document.createElement('td'); td2.textContent=s.note||'';
    const td3=document.createElement('td'); td3.textContent=fmt(s.expires);
    const td4=document.createElement('td'); td4.innerHTML=exp;
    const td5=document.createElement('td');
    const cp=document.createElement('button'); cp.className='ghost'; cp.textContent='复制'; cp.onclick=()=>copyText(url);
    const rv=document.createElement('button'); rv.className='danger'; rv.textContent='撤销'; rv.style.marginLeft='6px'; rv.onclick=()=>revoke(s.id);
    td5.appendChild(cp); td5.appendChild(rv);
    tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3); tr.appendChild(td4); tr.appendChild(td5);
    tb.appendChild(tr);
  });
}
async function createShare(){
  const pw=document.getElementById('pw').value;
  const min=parseInt(document.getElementById('min').value||'60',10);
  const note=document.getElementById('note').value;
  const msg=document.getElementById('shareMsg');
  if(!pw){ msg.className='msg err'; msg.textContent='请填写访问密码'; return; }
  const r=await api('/console/api/shares',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw,minutes:min,note})});
  const d=await r.json();
  if(d.success){ const url=location.origin+'/share?id='+d.id; msg.className='msg ok'; msg.textContent='已生成，链接如下：';
    document.getElementById('shareUrl').value=url; document.getElementById('shareResult').style.display='flex'; loadShares(); }
  else { msg.className='msg err'; msg.textContent=d.error||'生成失败'; }
}
function copyUrl(){ copyText(document.getElementById('shareUrl').value); }
function copyText(t){ if(navigator.clipboard){ navigator.clipboard.writeText(t).catch(()=>{}); } }
async function revoke(id){
  if(!confirm('确认撤销该分享链接？')) return;
  const r=await api('/console/api/shares/'+id,{method:'DELETE'});
  const d=await r.json(); if(d.success) loadShares();
}
async function loadNodes(){
  try{ const r=await api('/console/api/nodes'); const d=await r.json(); renderNodes(d.nodes||[]); }
  catch(e){ console.error(e); }
}
function renderNodes(list){
  const tb=document.querySelector('#nodeTable tbody'); tb.innerHTML='';
  list.forEach(n=>{
    const tr=document.createElement('tr');
    const td1=document.createElement('td'); td1.textContent=n.name||'';
    const td2=document.createElement('td'); td2.textContent=n.location||'';
    const td3=document.createElement('td'); td3.innerHTML='<code>'+(n.url||'')+'</code>';
    const td4=document.createElement('td'); td4.innerHTML=n.current?'<span class="badge ok">本机</span>':'-';
    const td5=document.createElement('td');
    const del=document.createElement('button'); del.className='danger'; del.textContent='删除'; del.onclick=()=>delNode(n.id);
    td5.appendChild(del);
    tr.appendChild(td1); tr.appendChild(td2); tr.appendChild(td3); tr.appendChild(td4); tr.appendChild(td5);
    tb.appendChild(tr);
  });
}
async function addNode(){
  const name=document.getElementById('nName').value;
  const loc=document.getElementById('nLoc').value;
  const url=document.getElementById('nUrl').value;
  const msg=document.getElementById('nodeMsg');
  if(!name||!loc||!url){ msg.className='msg err'; msg.textContent='请填写名称、位置、URL'; return; }
  const r=await api('/console/api/nodes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,location:loc,url})});
  const d=await r.json();
  if(d.success){ msg.className='msg ok'; msg.textContent='节点已添加'; document.getElementById('nName').value='';document.getElementById('nLoc').value='';document.getElementById('nUrl').value=''; loadNodes(); }
  else { msg.className='msg err'; msg.textContent=d.error||'添加失败'; }
}
async function delNode(id){
  if(!confirm('确认删除该节点？')) return;
  const r=await api('/console/api/nodes/'+id,{method:'DELETE'});
  const d=await r.json(); if(d.success) loadNodes();
}
async function setPw(){
  const old=document.getElementById('oldPw').value;
  const nw=document.getElementById('newPw').value;
  const msg=document.getElementById('pwMsg');
  const r=await api('/console/api/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old,new:nw})});
  const d=await r.json();
  if(d.success){ msg.className='msg ok'; msg.textContent='密码已更新'; document.getElementById('oldPw').value='';document.getElementById('newPw').value=''; }
  else { msg.className='msg err'; msg.textContent=d.error||'更新失败'; }
}
async function loadTools(){
  try{ const r=await api('/console/api/tools'); const d=await r.json(); renderTools(d.tools||[]); }
  catch(e){ console.error(e); }
}
function renderTools(list){
  const box=document.getElementById('toolsList'); box.innerHTML='';
  list.forEach(t=>{
    const row=document.createElement('div'); row.style.cssText='display:flex;align-items:center;justify-content:space-between';
    const lbl=document.createElement('span'); lbl.textContent=t.label; lbl.style.cssText='font-size:13px';
    const sw=document.createElement('label'); sw.style.cssText='display:inline-flex;align-items:center;gap:6px;cursor:pointer';
    const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=!!t.enabled; cb.dataset.id=t.id; cb.style.cssText='width:16px;height:16px;margin:0';
    sw.appendChild(cb); sw.appendChild(document.createTextNode('开放'));
    row.appendChild(lbl); row.appendChild(sw); box.appendChild(row);
  });
}
async function saveTools(){
  const cbs=document.querySelectorAll('#toolsList input[type=checkbox]');
  const tools={}; cbs.forEach(c=>{ tools[c.dataset.id]=c.checked; });
  const msg=document.getElementById('toolsMsg');
  const r=await api('/console/api/tools',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({tools})});
  const d=await r.json();
  if(d.success){ msg.className='msg ok'; msg.textContent='已保存'; loadTools(); }
  else { msg.className='msg err'; msg.textContent=d.error||'保存失败'; }
}
loadShares(); loadNodes(); loadTools();
</script>
</body>
</html>
"""

# Inject light/dark theme toggle into gateway-rendered pages. They all use
# CSS custom properties; switching `data-theme` on <html> re-colors everything.
LOGIN_HTML = LOGIN_HTML.replace("</head>", THEME_MODE_HEAD + "</head>", 1).replace("</body>", THEME_MODE_BODY + GW_BAR_SNIPPET + "</body>", 1)
SHARE_HTML = SHARE_HTML.replace("</head>", THEME_MODE_HEAD + "</head>", 1).replace("</body>", THEME_MODE_BODY + GW_BAR_SNIPPET + "</body>", 1)
CONSOLE_LOGIN_HTML = CONSOLE_LOGIN_HTML.replace("</head>", THEME_MODE_HEAD + "</head>", 1).replace("</body>", THEME_MODE_BODY + "</body>", 1)
CONSOLE_HTML = CONSOLE_HTML.replace("</head>", THEME_MODE_HEAD + "</head>", 1).replace("</body>", THEME_MODE_BODY + "</body>", 1)


# --------------------------------------------------------------------------
def _iter_chunked(sock, timeout=None):
    """Yield clean (de-chunked) byte blocks from a chunked HTTP body on `sock`.
    Stops when the terminating 0-length chunk is seen or the socket closes."""
    sock.settimeout(timeout)
    buf = b""
    closed = False
    while True:
        while b"\r\n" not in buf:
            if closed:
                return
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                chunk = b""
            if not chunk:
                closed = True
                if not buf:
                    return
                break
            buf += chunk
        if b"\r\n" not in buf:
            return
        line, buf = buf.split(b"\r\n", 1)
        line = line.strip()
        if not line:
            continue
        try:
            size = int(line.split(b";")[0], 16)
        except ValueError:
            size = 0
        if size == 0:
            # terminating chunk: consume trailing CRLF (and any trailers)
            while b"\r\n" not in buf:
                if closed:
                    return
                c = sock.recv(8192)
                if not c:
                    return
                buf += c
            buf = buf.split(b"\r\n", 1)[1]
            return
        while len(buf) < size + 2 and not closed:
            c = sock.recv(8192)
            if not c:
                closed = True
                break
            buf += c
        data = buf[:size]
        buf = buf[size + 2:] if len(buf) >= size + 2 else b""
        if data:
            yield data


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "NM-Gateway/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---- helpers ----
    def _send(self, code, body=b"", headers=None, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        # Always send Content-Length (even 0) + Connection: close so HTTP/1.1
        # clients don't block waiting for a body that never arrives.
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            if isinstance(v, list):
                for iv in v:
                    self.send_header(k, iv)
            else:
                self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _cookie_val(self, name):
        ch = self.headers.get("Cookie", "")
        for part in ch.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return urllib.parse.unquote(part[len(name) + 1:])
        return None

    def _authed(self):
        if session_user(self._cookie_val(COOKIE_NAME)):
            return True
        sid = self._cookie_val(SHARE_COOKIE)
        return bool(sid) and _share_valid(sid)

    def _serve_login(self, code=200, msg=""):
        # NOTE: do not use str.format() here - the HTML/CSS contains literal
        # { } braces that would break .format(). Use a plain replace instead.
        self._send(code, LOGIN_HTML.replace("{msg}", msg).encode("utf-8"))

    # ---- share-link access (password + expiry) ----
    def _serve_share(self, share_id, err=""):
        # NOTE: only {id} and {msg} are replaced; other braces are literal CSS.
        html = SHARE_HTML.replace("{id}", share_id or "").replace("{msg}", err)
        self._send(200, html.encode("utf-8"))

    def _serve_expired(self):
        self._send(200, EXPIRED_HTML.encode("utf-8"))

    def _handle_share_get(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sid = (params.get("id") or [""])[0]
        load_shares()
        if not sid:
            self._serve_share(sid, "缺少链接参数")
            return
        s = SHARES.get(sid)
        if not s:
            self._serve_share(sid, "链接无效")
            return
        if s.get("exp", 0) < time.time():
            self._serve_expired()
            return
        self._serve_share(sid)

    def _handle_share_post(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        data = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        sid = (data.get("id") or [""])[0]
        pw = (data.get("pass") or [""])[0]
        if _verify_share_pw(sid, pw):
            rem = int(max(0, SHARES[sid]["exp"] - time.time()))
            self._send(302, b"", {
                "Location": "/",
                "Set-Cookie": f"{SHARE_COOKIE}={sid}; Path=/; HttpOnly; Max-Age={rem}; SameSite=Lax",
            })
        else:
            self._serve_share(sid, "密码错误")

    # ---- compression / caching helpers ----
    def _gzip_ok(self):
        return "gzip" in (self.headers.get("Accept-Encoding", "") or "").lower()

    def _is_compressible(self, ctype):
        c = (ctype or "").lower()
        return any(c.startswith(t) for t in COMPRESSIBLE)

    def _cache_header(self, path):
        # hashed, content-addressed assets -> cache forever (safe: filename changes on edit)
        if HASHED_ASSET_RE.search(path or ""):
            return "public, max-age=31536000, immutable"
        return None

    # ---- raw streaming reverse proxy (handles SSE / chunked / keep-alive) ----
    def _proxy(self, require_auth, path_override=None, extra_headers=None):
        if require_auth and not self._authed():
            self._serve_login(200)
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else None

        # ---- tool access control ----
        # Blocks a disabled network tool at the edge (403) regardless of whether
        # the upstream still reports it as available. This is the single control
        # point for "which tools are open / which are forbidden".
        tool = tool_for_path(self.path.split("?")[0])
        if tool and not tool_enabled(tool):
            self._send_json(403, {"success": False,
                                  "error": f"工具 '{tool}' 已被管理员禁用"})
            return

        # ---- WebSocket tunneling (e.g. interactive shell) ----
        # The Shell feature upgrades to a WebSocket; the default handler cannot
        # relay it, so we tunnel raw bytes bidirectionally instead.
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._proxy_ws()
            return

        try:
            up = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10)
        except Exception as ex:
            self._send(502, f"Bad gateway (upstream {UPSTREAM_HOST}:{UPSTREAM_PORT}): {ex}".encode())
            return

        # NetMirror's /method/<tool> endpoints are synchronous long-polls: the
        # backend does not send HTTP response headers until the tool (mtr etc.)
        # finishes, which can take up to its 60s timeout. Use a matching read
        # timeout so the gateway does not return 502 while upstream is still
        # actively working.
        target_path = path_override or self.path.split("?")[0]
        long_poll = target_path.startswith("/method/")
        header_timeout = 75 if long_poll else 15

        # (re)build the request line + headers for the upstream
        target_path = path_override or self.path
        # X-Forwarded-For / X-Real-IP are handled by us (see below) so they are
        # skipped here to avoid leaking/duplicating a hop we don't control.
        skip = ("host", "connection", "content-length", "transfer-encoding",
                "upgrade", "proxy-connection", "accept-encoding",
                "x-forwarded-for", "x-real-ip")
        lines = [f"{self.command} {target_path} HTTP/1.1"]
        for k in self.headers.keys():
            if k.lower() in skip:
                continue
            lines.append(f"{k}: {self.headers[k]}")
        lines.append(f"Host: {UPSTREAM_HOST}:{UPSTREAM_PORT}")
        lines.append("Connection: close")
        # We re-compress ourselves; ask upstream for uncompressed so we can
        # measure/compress deterministically (also avoids double-compression).
        lines.append("Accept-Encoding: identity")
        for hk, hv in (extra_headers or {}).items():
            lines.append(f"{hk}: {hv}")
        # Forward the REAL visitor IP to the upstream panel so its c.ClientIP()
        # returns the actual client address instead of the gateway/Docker bridge
        # IP (e.g. 172.17.0.1). The gateway is the edge reverse proxy facing
        # the public internet, so self.client_address is the true client.
        client_ip = self.client_address[0]
        lines.append(f"X-Forwarded-For: {client_ip}")
        lines.append(f"X-Real-IP: {client_ip}")
        # When we forward a body, send an explicit Content-Length so the upstream
        # (gin) parses it correctly instead of relying on connection-close EOF.
        if body is not None:
            lines.append(f"Content-Length: {len(body)}")
        req = ("\r\n".join(lines) + "\r\n\r\n").encode()
        try:
            up.sendall(req)
            if body:
                up.sendall(body)

            # read response status line + headers until blank line
            up.settimeout(header_timeout)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = up.recv(4096)
                if not chunk:
                    break
                buf += chunk
            header_blob, _, rest = buf.partition(b"\r\n\r\n")
            parts = header_blob.split(b"\r\n")
            status_line = parts[0].decode("latin1")
            try:
                status = int(status_line.split(" ", 2)[1])
            except Exception:
                status = 502

            # parse response headers (skip hop-by-hop), capture content-type
            send_headers = []
            content_type = ""
            for line in parts[1:]:
                if b":" not in line:
                    continue
                k, v = line.split(b":", 1)
                kl = k.decode("latin1").lower()
                if kl in ("connection", "transfer-encoding", "keep-alive", "server"):
                    continue
                vk = v.decode("latin1").strip()
                send_headers.append((k.decode("latin1"), vk))
                if kl == "content-type":
                    content_type = vk

            # In PANEL mode, inject a floating logout button + boot spinner into HTML.
            is_html = (not AGENT_MODE) and ("text/html" in content_type.lower())

            # The /session SSE carries the node's `Config` (feature flags) that the
            # panel UI uses to show/hide tools. When tools are disabled here, rewrite
            # those flags so the UI reflects the policy (in addition to the request
            # time 403 check), giving a coherent "forbidden" experience.
            if target_path.rstrip("/") == "/session" and "text/event-stream" in content_type.lower():
                self._proxy_sse_rewrite(up, status, send_headers, rest)
                return

            # capture upstream Content-Length (0 if chunked/unknown)
            cl = 0
            for k, v in send_headers:
                if k.lower() == "content-length":
                    try:
                        cl = int(v)
                    except ValueError:
                        cl = 0

            if is_html:
                # buffer the body, inject, send with fixed Content-Length.
                # Read exactly Content-Length bytes if known; otherwise read with
                # an idle timeout so a keep-alive upstream cannot hang the page load.
                data = bytearray(rest)
                up.settimeout(10)
                if cl > 0:
                    while len(data) < cl:
                        chunk = up.recv(min(8192, cl - len(data)))
                        if not chunk:
                            break
                        data += chunk
                else:
                    while True:
                        try:
                            chunk = up.recv(8192)
                        except socket.timeout:
                            break
                        if not chunk:
                            break
                        data += chunk
                try:
                    html = data.decode("utf-8")
                except Exception:
                    html = data.decode("latin1")
                html = self._inject_spinner(html)
                html = self._inject_logout(html)
                out = html.encode("utf-8")
                # drop hop-by-hop + length/encoding headers; we re-send plain text
                send_headers = [(k, v) for (k, v) in send_headers
                               if k.lower() not in
                               ("content-length", "transfer-encoding", "content-encoding")]
                # cache: html pages revalidate; never cache the login page body hard
                ch = self._cache_header(self.path) or "no-cache"
                send_headers.append(("Cache-Control", ch))
                out = self._maybe_gzip(out, send_headers)
                self.send_response(status)
                for k, v in send_headers:
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.close_connection = True
                self.wfile.write(out)
                self.wfile.flush()
            else:
                compressible = (self._is_compressible(content_type)
                                and cl > 0 and cl <= MAX_COMPRESS
                                and self._gzip_ok())
                if compressible:
                    # buffer full body, compress, send with fixed headers
                    data = bytearray(rest)
                    while len(data) < cl:
                        chunk = up.recv(min(8192, cl - len(data)))
                        if not chunk:
                            break
                        data += chunk
                    body_bytes = bytes(data)
                    out_hdrs = [(k, v) for (k, v) in send_headers
                                if k.lower() not in
                                ("content-length", "transfer-encoding", "content-encoding")]
                    ch = self._cache_header(self.path)
                    if ch:
                        out_hdrs.append(("Cache-Control", ch))
                    body_bytes = self._maybe_gzip(body_bytes, out_hdrs)
                    self.send_response(status)
                    for k, v in out_hdrs:
                        self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.send_header("Content-Length", str(len(body_bytes)))
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.write(body_bytes)
                    self.wfile.flush()
                else:
                    # stream verbatim (SSE / JSON / large / no-gzip) - do NOT buffer
                    self.send_response(status)
                    for k, v in send_headers:
                        self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    if rest:
                        self.wfile.write(rest)
                        self.wfile.flush()
                    up.settimeout(None)
                    try:
                        while True:
                            chunk = up.recv(8192)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        # client (browser / SPA SSE reconnect) disconnected early
                        pass
        except Exception as ex:
            import traceback as _tb
            sys.stderr.write("GATEWAY PROXY ERROR:\n" + _tb.format_exc() + "\n")
            sys.stderr.flush()
            try:
                self._send(502, f"Bad gateway: {ex}".encode())
            except Exception:
                pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _maybe_gzip(self, data, send_headers):
        """Gzip data in place (mutating send_headers) if worthwhile + accepted."""
        if not self._gzip_ok() or len(data) < 1024:
            return data
        try:
            gz = gzip.compress(data, 6)
        except Exception:
            return data
        # only use gzip if it actually helps
        if len(gz) >= len(data):
            return data
        send_headers.append(("Content-Encoding", "gzip"))
        send_headers.append(("Vary", "Accept-Encoding"))
        return gz

    # ---- inject a pure-CSS boot spinner into #app (kills the blank white screen) ----
    BOOT_SPINNER_STYLE = (
        '<style id="nm-boot-style">'
        '#nm-boot{position:fixed;inset:0;z-index:99999;display:flex;flex-direction:column;'
        'align-items:center;justify-content:center;gap:18px;'
        'background:linear-gradient(160deg,#f3f6ff 0%,#e7edff 100%);'
        'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}'
        'html.dark #nm-boot{background:linear-gradient(160deg,#0b1020 0%,#16203a 100%)}'
        '#nm-boot .nm-spinner{width:44px;height:44px;border-radius:50%;'
        'border:4px solid rgba(79,140,255,.25);border-top-color:var(--accent,#4f8cff);'
        'animation:nm-spin .8s linear infinite}'
        'html.dark #nm-boot .nm-spinner{border-color:rgba(79,140,255,.15)}'
        '#nm-boot .nm-tip{color:#5a6b8c;font-size:14px;letter-spacing:.04em}'
        'html.dark #nm-boot .nm-tip{color:#8b97b3}'
        '@keyframes nm-spin{to{transform:rotate(360deg)}}'
        '</style>'
    )
    BOOT_SPINNER_BODY = (
        '<div id="nm-boot">'
        '<script>(function(){try{if(localStorage.theme==="dark")document.documentElement.classList.add("dark");}catch(e){}})();</script>'
        '<div class="nm-spinner"></div>'
        '<div class="nm-tip">正在加载 NetMirror…</div>'
        '</div>'
    )

    def _inject_spinner(self, html):
        # Only inject into the SPA shell (has an empty #app); the login page has
        # its own content and is left untouched.
        if '<div id="app"></div>' in html:
            html = html.replace('<div id="app"></div>',
                                '<div id="app">' + self.BOOT_SPINNER_BODY + '</div>', 1)
            if '</head>' in html:
                html = html.replace('</head>', self.BOOT_SPINNER_STYLE + '</head>', 1)
            else:
                html = self.BOOT_SPINNER_STYLE + html
        return html

    # ---- inject a floating top-right bar with console + logout links ----
    def _inject_logout(self, html):
        snippet = GW_BAR_SNIPPET
        if "</body>" in html:
            return html.replace("</body>", snippet + "</body>", 1)
        return html + snippet

    # ---- WebSocket tunneling (e.g. interactive Shell) ----
    def _proxy_ws(self):
        """Raw bidirectional WebSocket tunnel to upstream.
        The Shell feature upgrades to a WebSocket; the default HTTP handler
        cannot relay it, so we forward the handshake and copy bytes both ways.
        Auth was already enforced by the caller (_proxy)."""
        try:
            up = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=10)
        except Exception as ex:
            self._send(502, f"Bad gateway (upstream {UPSTREAM_HOST}:{UPSTREAM_PORT}): {ex}".encode())
            return
        try:
            skip = ("host", "connection", "proxy-connection", "accept-encoding",
                    "content-length", "transfer-encoding",
                    "x-forwarded-for", "x-real-ip")
            lines = [f"GET {self.path} HTTP/1.1"]
            for k in self.headers.keys():
                if k.lower() in skip:
                    continue
                lines.append(f"{k}: {self.headers[k]}")
            lines.append(f"Host: {UPSTREAM_HOST}:{UPSTREAM_PORT}")
            lines.append("Connection: Upgrade")
            lines.append("Upgrade: websocket")
            lines.append("Accept-Encoding: identity")
            if "Sec-WebSocket-Key" not in self.headers:
                key = base64.b64encode(os.urandom(16)).decode()
                lines.append(f"Sec-WebSocket-Key: {key}")
            if "Sec-WebSocket-Version" not in self.headers:
                lines.append("Sec-WebSocket-Version: 13")
            client_ip = self.client_address[0]
            lines.append(f"X-Forwarded-For: {client_ip}")
            lines.append(f"X-Real-IP: {client_ip}")
            req = ("\r\n".join(lines) + "\r\n\r\n").encode()
            up.sendall(req)
            up.settimeout(15)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = up.recv(4096)
                if not chunk:
                    raise RuntimeError("upstream closed before handshake")
                buf += chunk
            header_blob, _, rest = buf.partition(b"\r\n\r\n")
            # relay upstream handshake verbatim to the client
            self.wfile.write(header_blob + b"\r\n\r\n")
            if rest:
                self.wfile.write(rest)
            self.wfile.flush()
            self.close_connection = True
            self._tunnel(self.connection, up)
        except Exception as ex:
            sys.stderr.write("GATEWAY WS ERROR: %r\n" % (ex,))
            sys.stderr.flush()
            try:
                self._send(502, f"Bad gateway: {ex}".encode())
            except Exception:
                pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _tunnel(self, a, b):
        """Bidirectional raw byte copy between sockets a and b until both close."""
        a.setblocking(False)
        b.setblocking(False)
        try:
            while True:
                r, _, _ = select.select([a, b], [], [], 30)
                if not r:
                    continue
                for src, dst in ((a, b), (b, a)):
                    if src in r:
                        try:
                            data = src.recv(65536)
                        except (BlockingIOError, socket.timeout, OSError):
                            continue
                        if not data:
                            # one side closed; stop relaying
                            return
                        try:
                            dst.sendall(data)
                        except OSError:
                            return
        except Exception:
            pass

    # ---- /session SSE feature-flag rewrite (hide disabled tools in the UI) ----
    def _proxy_sse_rewrite(self, up, status, send_headers, rest=b""):
        """Stream upstream /session SSE, but force disabled tool feature flags
        to false so the panel UI hides those tools (coherent with the request
        time 403 block). Default (no tool disabled) streams verbatim, preserving
        the upstream Transfer-Encoding so the client can de-chunk correctly."""
        disabled = {TOOL_FEATURE[t] for t in TOOL_IDS if not tool_enabled(t)}
        is_chunked = any(k.lower() == "transfer-encoding" and "chunked" in v.lower()
                         for k, v in send_headers)
        self.send_response(status)
        for k, v in send_headers:
            kl = k.lower()
            if kl in ("content-length", "content-encoding"):
                continue
            if kl == "transfer-encoding" and disabled:
                # rewrite path de-chunks itself, so drop the header
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.flush()
        try:
            if not disabled:
                # fast path: forward the (possibly chunked) stream verbatim,
                # including the bytes already read past the header boundary.
                if rest:
                    self.wfile.write(rest)
                    self.wfile.flush()
                up.settimeout(None)
                while True:
                    chunk = up.recv(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                return
            # rewrite path: de-chunk (if needed), rewrite Config lines, forward clean.
            # `rest` holds the bytes already buffered past the header boundary.
            if is_chunked:
                src = _iter_chunked(up)
            else:
                def _plain():
                    up.settimeout(None)
                    while True:
                        c = up.recv(8192)
                        if not c:
                            break
                        yield c
                src = _plain()
            buf = rest
            for clean in src:
                buf += clean
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    out = self._rewrite_sse_line(line, disabled)
                    self.wfile.write(out + b"\n")
                    self.wfile.flush()
            if buf:
                self.wfile.write(buf)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _rewrite_sse_line(self, line, disabled):
        s = line
        stripped = s.lstrip()
        if stripped.startswith(b"data:"):
            payload = s.split(b"data:", 1)[1].strip()
            if payload and b"feature_" in payload:
                try:
                    obj = json.loads(payload)
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    modified = False
                    for f in disabled:
                        if f in obj and obj[f] is not False:
                            obj[f] = False
                            modified = True
                    if modified:
                        prefix = s.split(b"data:", 1)[0] + b"data: "
                        return prefix + json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return s

# ---- admin console ----
    def _admin_authed(self):
        tok = self._cookie_val(ADMIN_COOKIE)
        if not tok:
            return False
        s = ADMIN_SESSIONS.get(tok)
        if not s:
            return False
        if s.get("exp", 0) < time.time():
            ADMIN_SESSIONS.pop(tok, None)
            save_admin_sessions()
            return False
        return True

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, ctype="application/json; charset=utf-8")

    def _save_shares(self):
        # atomic write so the management tool / reload sees a consistent file
        tmp = SHARES_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(SHARES, f, indent=2)
            os.replace(tmp, SHARES_FILE)
            global SHARES_MTIME
            SHARES_MTIME = os.path.getmtime(SHARES_FILE)
        except Exception:
            pass

    def _serve_console_login(self, msg=""):
        # NOTE: only {msg} is replaced; other braces are literal CSS.
        self._send(200, CONSOLE_LOGIN_HTML.replace("{msg}", msg).encode("utf-8"))

    def _serve_console(self):
        self._send(200, CONSOLE_HTML.encode("utf-8"))

    def _handle_console_post(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        data = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        action = (data.get("action") or [""])[0]
        if action == "login":
            key = (data.get("key") or [""])[0]
            if _console_key() and key == _console_key():
                tok = secrets.token_hex(32)
                ADMIN_SESSIONS[tok] = {"exp": time.time() + ADMIN_TTL}
                save_admin_sessions()
                self._send(302, b"", {
                    "Location": "/console",
                    "Set-Cookie": f"{ADMIN_COOKIE}={tok}; Path=/; HttpOnly; Max-Age={ADMIN_TTL}; SameSite=Lax",
                })
            else:
                self._serve_console_login(msg="密钥错误")
        else:
            self._send(400, b"bad action")

    def _shares_list(self):
        load_shares()
        now = time.time()
        out = []
        for sid, s in SHARES.items():
            exp = s.get("exp", 0)
            out.append({
                "id": sid,
                "note": s.get("note", ""),
                "created": s.get("created", 0),
                "expires": exp,
                "remaining": max(0, int(exp - now)),
                "expired": exp < now,
            })
        out.sort(key=lambda x: x["expires"], reverse=True)
        self._send_json(200, {"success": True, "shares": out})

    def _shares_create(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            data = {}
        pw = str(data.get("password", "") or "")
        try:
            minutes = int(data.get("minutes", 60))
        except Exception:
            minutes = 60
        if minutes <= 0:
            minutes = 60
        if minutes > 60 * 24 * 30:
            minutes = 60 * 24 * 30
        note = str(data.get("note", "") or "")
        if not pw:
            self._send_json(400, {"success": False, "error": "密码不能为空"})
            return
        sid = secrets.token_urlsafe(16)
        salt = secrets.token_hex(16)
        phash = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), SHARE_PW_ITER).hex()
        now = int(time.time())
        exp = now + minutes * 60
        load_shares()
        SHARES[sid] = {"salt": salt, "phash": phash, "exp": exp, "created": now, "note": note}
        self._save_shares()
        self._send_json(200, {"success": True, "id": sid, "created": now, "expires": exp, "note": note})

    def _shares_revoke(self, sid):
        load_shares()
        if sid in SHARES:
            del SHARES[sid]
            self._save_shares()
            self._send_json(200, {"success": True})
        else:
            self._send_json(404, {"success": False, "error": "链接不存在"})

    def _console_set_password(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            data = {}
        old = str(data.get("old", "") or "")
        new = str(data.get("new", "") or "")
        cur = _console_key()
        if cur and old != cur:
            self._send_json(403, {"success": False, "error": "当前密码错误"})
            return
        if len(new) < 4:
            self._send_json(400, {"success": False, "error": "新密码至少4位"})
            return
        try:
            with open(ADMIN_KEY_FILE, "w") as f:
                f.write(new.strip())
            global _console_key_cache
            _console_key_cache = {"val": new.strip(), "mtime": os.path.getmtime(ADMIN_KEY_FILE)}
            self._send_json(200, {"success": True})
        except Exception as ex:
            self._send_json(500, {"success": False, "error": str(ex)})

    def _tools_get(self):
        load_tools()  # refresh from disk
        out = []
        for tid in TOOL_BASE_IDS:
            out.append({
                "id": tid,
                "label": TOOL_UI_LABELS.get(tid, tid),
                "enabled": tool_enabled(tid),
            })
        self._send_json(200, {"success": True, "tools": out, "file": _tools_file()})

    def _tools_put(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            data = {}
        tools_obj = data.get("tools", {})
        if not isinstance(tools_obj, dict):
            self._send_json(400, {"success": False, "error": "tools 必须是对象"})
            return
        state = {}
        for tid in TOOL_BASE_IDS:
            state[tid] = bool(tools_obj.get(tid, True))
        tf = _tools_file()
        d = os.path.dirname(tf)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        tmp = tf + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"tools": state}, f, indent=2, ensure_ascii=False)
            os.replace(tmp, tf)
            global _tools_cache
            _tools_cache = {"mtime": -1, "data": None}
            self._send_json(200, {"success": True, "tools": state})
        except Exception as ex:
            self._send_json(500, {"success": False, "error": str(ex)})

    def _route_console_api(self):
        if not self._admin_authed():
            self._send_json(403, {"success": False, "error": "未授权"})
            return
        p = self.path.split("?")[0]
        method = self.command
        # node management
        if p == "/console/api/nodes":
            if method == "GET":
                # public list endpoint (no key required)
                self._proxy(require_auth=False, path_override="/nodes")
            elif method == "POST":
                self._proxy(require_auth=False, path_override="/api/admin/nodes",
                            extra_headers={"X-Api-Key": _panel_key()})
            else:
                self._send_json(405, {"success": False, "error": "method not allowed"})
            return
        if p.startswith("/console/api/nodes/"):
            nid = p.rsplit("/", 1)[-1]
            if method == "DELETE":
                self._proxy(require_auth=False, path_override=f"/api/admin/nodes/{nid}",
                            extra_headers={"X-Api-Key": _panel_key()})
            else:
                self._send_json(405, {"success": False, "error": "method not allowed"})
            return
        if p == "/console/api/shares":
            if method == "GET":
                self._shares_list()
            elif method == "POST":
                self._shares_create()
            else:
                self._send_json(405, {"success": False, "error": "method not allowed"})
            return
        if p.startswith("/console/api/shares/"):
            sid = p.rsplit("/", 1)[-1]
            if method == "DELETE":
                self._shares_revoke(sid)
            else:
                self._send_json(405, {"success": False, "error": "method not allowed"})
            return
        if p == "/console/api/password" and method == "POST":
            self._console_set_password()
            return
        if p == "/console/api/tools":
            if method == "GET":
                self._tools_get()
            elif method == "PUT":
                self._tools_put()
            else:
                self._send_json(405, {"success": False, "error": "method not allowed"})
            return
        self._send_json(404, {"success": False, "error": "not found"})

    # ---- helpers for agent mode access control ----
    def _agent_allowed(self):
        # If ALLOW_IPS is empty, agent is public (NetMirror UI needs browser access).
        if not ALLOW_IPS:
            return True
        return self.client_address[0] in ALLOW_IPS or self.client_address[0] == "127.0.0.1"

    # ---- handlers ----
    def do_GET(self):
        p = self.path.split("?")[0]
        if AGENT_MODE:
            if not self._agent_allowed():
                self._send(403, b"Forbidden")
                return
            if p in ("/login", "/logout"):
                self._send(404, b"not found")
                return
            self._proxy(require_auth=False)
            return
        # panel mode: only /login and /logout are open; everything else
        # (UI, /api, /session, /method) requires a valid login session cookie,
        # EXCEPT requests coming from a trusted peer panel IP (server-to-server
        # calls that keep dual-panel mutual management working).
        peer = self.client_address[0] in PEER_IPS
        if p.startswith("/console/api/"):
            self._route_console_api()
            return
        if p == "/login":
            self._serve_login()
        elif p == "/logout":
            tok = self._cookie_val(COOKIE_NAME)
            if tok:
                drop_session(tok)
            at = self._cookie_val(ADMIN_COOKIE)
            if at:
                ADMIN_SESSIONS.pop(at, None)
                save_admin_sessions()
            clear = [f"{COOKIE_NAME}=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax"]
            sc = self._cookie_val(SHARE_COOKIE)
            if sc:
                clear.append(f"{SHARE_COOKIE}=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax")
            if at:
                clear.append(f"{ADMIN_COOKIE}=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax")
            self._send(302, b"", {"Location": "/login", "Set-Cookie": clear})
        elif p == "/share":
            self._handle_share_get()
        elif p == "/console/logout":
            at = self._cookie_val(ADMIN_COOKIE)
            if at:
                ADMIN_SESSIONS.pop(at, None)
                save_admin_sessions()
            self._send(302, b"", {
                "Location": "/console",
                "Set-Cookie": f"{ADMIN_COOKIE}=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax",
            })
        elif p == "/console":
            if self._admin_authed():
                self._serve_console()
            else:
                self._serve_console_login()
        else:
            if self._authed():
                self._proxy(require_auth=not peer)
            else:
                sid = self._cookie_val(SHARE_COOKIE)
                if sid and not _share_valid(sid):
                    self._serve_expired()
                else:
                    self._serve_login()

    def do_POST(self):
        p = self.path.split("?")[0]
        if AGENT_MODE:
            if not self._agent_allowed():
                self._send(403, b"Forbidden")
                return
            self._proxy(require_auth=False)
            return
        peer = self.client_address[0] in PEER_IPS
        if p.startswith("/console/api/"):
            self._route_console_api()
            return
        if p == "/login":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b""
            data = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            user = (data.get("user") or [""])[0]
            pw = (data.get("pass") or [""])[0]
            if verify(user, pw):
                tok = new_session(user)
                self._send(302, b"", {
                    "Location": "/",
                    "Set-Cookie": f"{COOKIE_NAME}={tok}; Path=/; HttpOnly; Max-Age={SESSION_TTL}; SameSite=Lax",
                })
            else:
                self._serve_login(200, "账号或密码错误")
        elif p == "/share":
            self._handle_share_post()
        elif p == "/console":
            self._handle_console_post()
        else:
            self._proxy(require_auth=not peer)

    def do_PUT(self):
        if AGENT_MODE:
            if not self._agent_allowed():
                self._send(403, b"Forbidden")
                return
            self._proxy(require_auth=False)
            return
        if self.path.split("?")[0].startswith("/console/api/"):
            self._route_console_api()
            return
        self._proxy(require_auth=not (self.client_address[0] in PEER_IPS))

    def do_DELETE(self):
        if AGENT_MODE:
            if not self._agent_allowed():
                self._send(403, b"Forbidden")
                return
            self._proxy(require_auth=False)
            return
        if self.path.split("?")[0].startswith("/console/api/"):
            self._route_console_api()
            return
        self._proxy(require_auth=not (self.client_address[0] in PEER_IPS))

    def do_OPTIONS(self):
        # CORS preflight: pass through to upstream so it can reply 204 with headers.
        if AGENT_MODE:
            if not self._agent_allowed():
                self._send(403, b"Forbidden")
                return
            self._proxy(require_auth=False)
            return
        if self.path.split("?")[0].startswith("/console/api/"):
            self._route_console_api()
            return
        self._proxy(require_auth=not (self.client_address[0] in PEER_IPS))


def main():
    load_sessions()
    load_shares()
    load_admin_sessions()
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        if AGENT_MODE:
            mode = "AGENT(allow=%s)" % ",".join(ALLOW_IPS) if ALLOW_IPS else "AGENT(public)"
        else:
            mode = "PANEL(login)"
        print(f"NM gateway on :{PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}  [{mode}]")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
