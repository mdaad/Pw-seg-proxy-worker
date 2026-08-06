
#!/usr/bin/env python3
"""
PW Proxy - Optimised
Play  : /play/...   (pass-through proxy)
Download: /m3u8/<sid>/... + /seg/<sid> (server decrypt)
"""

import dns.resolver
dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
dns.resolver.default_resolver.nameservers = ['8.8.8.8', '1.1.1.1']

from flask import Flask, request, Response, jsonify
from flask_cors import CORS
from flask_compress import Compress
import requests as req_lib
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import logging
import re
import subprocess
import tempfile
import os
import hashlib
import secrets
import urllib.parse
import urllib.request
import urllib.error
import ssl
import random
import string
import gzip
import base64
import xml.etree.ElementTree as ET
import time as time_mod
import threading
import itertools
from urllib.parse import urlsplit, urlunsplit, quote, urljoin, parse_qs, urlencode
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

app = Flask(__name__)
Compress().init_app(app)
CORS(app, resources={r"/*": {"origins": "*", "methods": ["GET","POST","OPTIONS","HEAD"],
                              "allow_headers": ["*"], "expose_headers": ["*"]}})

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

DEFAULT_BATCH_ID = "67738e4a5787b05d8ec6e07f"
BASE             = "https://pwthor.live/api"
MONGODB_URI      = os.environ.get("MONGODB_URI", "mongodb+srv://cheetagaming559_db_user:APed2NSH9sKhZfY8@cluster0.lcn5a9q.mongodb.net/Delta-pw")

_DECRYPT_SEM     = threading.Semaphore(1)
ENABLE_PLAY      = True
ENABLE_DOWNLOAD  = True
DOWNLOAD_OFF_MSG = "Download feature is Currently off Due to Server Heavy load."
PLAY_OFF_MSG     = "Video playback is temporarily disabled."

# ── THREAD POOL for parallel tasks ───────────────────────────
_executor = ThreadPoolExecutor(max_workers=8)

# ── WORKER POOL ──────────────────────────────────────────────

WORKER_POOL = [
    "https://play.deltaverse.site",
    "https://play1.deltaverse.site",
    "https://play2.deltaverse.site",
    "https://play3.deltaverse.site",
    "https://play4.deltaverse.site",
    "https://play5.deltaverse.site",
]

_session_worker_map  = {}
_session_worker_lock = threading.Lock()
_worker_counter      = itertools.cycle(range(len(WORKER_POOL)))
_worker_lock         = threading.Lock()

def get_next_worker() -> str:
    with _worker_lock:
        idx = next(_worker_counter)
    return WORKER_POOL[idx % len(WORKER_POOL)]

def get_worker_for_session(session_id: str) -> str:
    if not session_id:
        return get_next_worker()
    with _session_worker_lock:
        if session_id in _session_worker_map:
            return _session_worker_map[session_id]
        worker = get_next_worker()
        _session_worker_map[session_id] = worker
        return worker

def build_worker_cf_url(full_url, session_id=None):
    full_url = _fix_url(full_url)
    p        = urlsplit(full_url)
    worker   = get_worker_for_session(session_id) if session_id else get_next_worker()
    out      = f"{worker}/cf/{p.netloc}{p.path}"
    return out + (f"?{p.query}" if p.query else "")

# ── HEADERS ──────────────────────────────────────────────────
PROXY_H = {
    "User-Agent":      "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
    "Referer":         "https://www.pw.live/",
    "Origin":          "https://www.pw.live",
    "Connection":      "keep-alive",
    "Accept-Encoding": "identity",
}
MANIFEST_H = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
    "Referer":    "https://www.pw.live/",
    "Origin":     "https://www.pw.live",
    "Connection": "keep-alive",
}

# ── HELPERS ──────────────────────────────────────────────────
def utc_now(): return datetime.now(timezone.utc)

def _fix_url(u):
    return u.replace("%7E", "~") if u else u

def get_public_base():
    proto = request.headers.get("X-Forwarded-Proto") or ("https" if request.is_secure else "http")
    host  = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.host
    return f"{proto}://{host}"

def extract_cloudfront_signed_url(url):
    url = _fix_url(url)
    if "cloudfront.net" in urlsplit(url).netloc:
        return url
    m = re.search(r'(d[a-z0-9]+\.cloudfront\.net/[^?]+)', url)
    if m:
        q   = urlsplit(url).query
        out = f"https://{m.group(1)}"
        return _fix_url(out + (f"?{q}" if q else ""))
    return url

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ✅ BUNNY CDN → CLOUDFRONT CONVERTER
# Policy param decode karke actual CF URL nikalta hai
# Sirf BunnyCDN URLs pe kaam karta hai, baaki as-is return
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def extract_cf_from_bunny(url: str) -> str:
    """
    BunnyCDN URL → CloudFront URL
    """
    if not url:
        return url

    try:
        parsed = urlsplit(url)

        # Sirf Bunny pe apply
        if 'b-cdn.net' not in parsed.netloc:
            return url

        params = parse_qs(parsed.query, keep_blank_values=True)
        policy_raw = params.get('Policy', [None])[0]

        if not policy_raw:
            print("❌ No Policy found in Bunny URL")
            return url

        # ✅ CORRECT reverse mapping
        # encoded: + -> -, = -> _, / -> ~
        # decode:  - -> +, _ -> =, ~ -> /
        policy_b64 = (
            policy_raw
            .replace('-', '+')
            .replace('_', '=')
            .replace('~', '/')
        )

        # padding fix
        policy_b64 += '=' * ((4 - len(policy_b64) % 4) % 4)

        decoded = base64.b64decode(policy_b64).decode('utf-8')
        policy_json = json.loads(decoded)

        resource = policy_json['Statement'][0]['Resource']
        cf_domain = urlsplit(resource).netloc

        if not cf_domain or 'cloudfront.net' not in cf_domain:
            print("❌ CF domain not found in decoded policy")
            print("Decoded policy:", decoded)
            return url

        cf_url = urlunsplit((
            'https',
            cf_domain,
            parsed.path,
            parsed.query,
            ''
        ))

        print("✅ Bunny decoded to CF:", cf_url)
        return _fix_url(cf_url)

    except Exception as e:
        print("❌ extract_cf_from_bunny failed:", str(e))
        print("Input URL:", url)
        return url
        
        
def normalize_video_url(url: str) -> str:
    """
    Koi bhi URL aaye - proper CF URL return karo
    BunnyCDN → CF decode
    CF → as-is
    Others → as-is
    """
    url = _fix_url(url)
    if not url:
        return url

    # BunnyCDN hai → decode karo
    if 'b-cdn.net' in url:
        decoded = extract_cf_from_bunny(url)
        if decoded != url:
            return decoded
        # Decode fail hua → original return
        return url

    # Already CloudFront → extract clean CF URL
    if 'cloudfront.net' in url:
        return extract_cloudfront_signed_url(url)

    return url


def build_proxy_manifest_url(base_url, original_url, session_id=None):
    original_url = normalize_video_url(original_url)
    p = urlsplit(original_url)
    # ✅ Same worker for entire video session
    worker = get_worker_for_session(session_id) if session_id else get_next_worker()
    url = f"{worker}/cf/{p.netloc}/{p.path.lstrip('/')}"
    return url + (f"?{p.query}" if p.query else "")
    
def ensure_signed_query(url, signed_query):
    if not signed_query: return url
    p = urlsplit(url); q = p.query
    if not q:
        q = signed_query
    elif "Signature=" not in q and "Policy=" not in q:
        q = q + "&" + signed_query
    return urlunsplit((p.scheme, p.netloc, p.path, q, ""))

def build_phone_play_url(full_url):
    full_url = _fix_url(full_url)
    p   = urlsplit(full_url)
    out = f"{get_public_base()}/play/{p.netloc}{p.path}"
    return out + (f"?{p.query}" if p.query else "")

def probe_native_hls(mpd_url):
    """MPD ke saath HLS bhi available hai? Background mein check karo"""
    try:
        if not mpd_url or "cloudfront.net" not in mpd_url: return ""
        p = urlsplit(mpd_url)
        if not p.path.endswith(".mpd"): return ""
        hls_url = _fix_url(urlunsplit((p.scheme, p.netloc, p.path[:-4]+".m3u8", p.query, "")))
        try:
            r = get_http().head(hls_url, headers=MANIFEST_H, timeout=5, allow_redirects=True)
            if r.status_code == 200: return hls_url
        except Exception: pass
        try:
            r = get_http().get(hls_url, headers=MANIFEST_H, timeout=8)
            if r.status_code == 200 and r.text.strip().startswith("#EXTM3U"): return hls_url
        except Exception: pass
    except Exception: pass
    return ""

# ── CONNECTION POOL ───────────────────────────────────────────
_http_session = None
_http_lock    = threading.Lock()

def get_http():
    global _http_session
    if _http_session is None:
        with _http_lock:
            if _http_session is None:
                s = req_lib.Session()
                a = HTTPAdapter(
                    pool_connections=20,
                    pool_maxsize=40,
                    max_retries=Retry(
                        total=2,
                        backoff_factor=0.2,
                        status_forcelist=[500,502,503,504],
                        allowed_methods=["GET","HEAD"]
                    ),
                    pool_block=False
                )
                s.mount("https://", a)
                s.mount("http://", a)
                s.headers.update({
                    "User-Agent":      "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
                    "Referer":         "https://www.pw.live/",
                    "Origin":          "https://www.pw.live",
                    "Connection":      "keep-alive",
                    "Accept-Encoding": "gzip, deflate"
                })
                _http_session = s
    return _http_session

# ── CACHES ───────────────────────────────────────────────────
class SimpleTTLCache:
    def __init__(self, max_items=100, ttl=300):
        self.cache     = {}
        self.max_items = max_items
        self.ttl       = ttl
        self.lock      = threading.Lock()

    def get(self, key):
        with self.lock:
            item = self.cache.get(key)
            if item is None: return None
            if time_mod.time() - item["t"] > self.ttl:
                self.cache.pop(key, None); return None
            return item["d"]

    def set(self, key, data):
        with self.lock:
            if len(self.cache) >= self.max_items:
                self.cache.pop(min(self.cache, key=lambda k: self.cache[k]["t"]), None)
            self.cache[key] = {"d": data, "t": time_mod.time()}

    def delete(self, key):
        with self.lock: self.cache.pop(key, None)


class SegmentCache:
    def __init__(self, max_items=200, ttl=1800):
        self.cache     = {}
        self.order     = []
        self.max_items = max_items
        self.ttl       = ttl
        self.lock      = threading.Lock()
        self.hits      = 0
        self.misses    = 0

    def get(self, key):
        with self.lock:
            item = self.cache.get(key)
            if item is None:
                self.misses += 1; return None
            if time_mod.time() - item["t"] > self.ttl:
                self.cache.pop(key, None)
                try: self.order.remove(key)
                except Exception: pass
                self.misses += 1; return None
            self.hits += 1; return item["d"]

    def set(self, key, data):
        with self.lock:
            if len(self.cache) >= self.max_items:
                for k in self.order[:20]: self.cache.pop(k, None)
                self.order = self.order[20:]
            if key not in self.cache: self.order.append(key)
            self.cache[key] = {"d": data, "t": time_mod.time()}

    def stats(self):
        total = self.hits + self.misses
        return {
            "size":     len(self.cache),
            "hits":     self.hits,
            "misses":   self.misses,
            "hit_rate": f"{self.hits/total*100:.1f}%" if total else "0%"
        }


MPD_CACHE      = SimpleTTLCache(30,  1800)
PLAY_MPD_CACHE = SimpleTTLCache(50,  600)
M3U8_CACHE     = SimpleTTLCache(50,  300)
SEGMENT_CACHE  = SegmentCache(200,   1800)
DECRYPT_CACHE  = SegmentCache(100,   1800)
VIDEO_INFO_CACHE = SimpleTTLCache(100, 1800)  # ✅ Video info cache

# ── MONGODB ───────────────────────────────────────────────────
sessions_col = None
try:
    mc = MongoClient(
        MONGODB_URI,
        server_api=ServerApi("1"),
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        tls=True,
        tlsAllowInvalidCertificates=True,
        maxPoolSize=5,
        minPoolSize=1
    )
    mc.admin.command("ping")
    sessions_col = mc["Delta-pw"]["pw_sessions"]
    print("✅ MongoDB connected")
except Exception as e:
    print(f"❌ MongoDB: {e}")

_sess_cache = {"data": [], "ts": 0, "lock": threading.Lock()}

def _load_sessions():
    with _sess_cache["lock"]:
        if time_mod.time() - _sess_cache["ts"] < 60:
            return _sess_cache["data"]
        data = []
        try:
            if sessions_col is not None:
                for q in [
                    {"is_active": True, "expires_at": {"$gt": utc_now()}},
                    {"is_active": True},
                    {}
                ]:
                    try:
                        data = list(sessions_col.find(q))
                        if data: break
                    except Exception: continue
        except Exception: pass
        clean = [x for x in data if isinstance(x, dict) and x.get("cookies")]
        _sess_cache["data"] = clean
        _sess_cache["ts"]   = time_mod.time()
        return clean

def get_random_session():
    a = [x for x in _load_sessions() if x.get("cookies")]
    return random.choice(a) if a else None

def count_active_sessions(): return len(_load_sessions())

# ── REDIRECT HANDLER ─────────────────────────────────────────
class RedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return urllib.request.Request(
            newurl, data=req.data, headers=dict(req.headers),
            origin_req_host=req.origin_req_host, unverifiable=True
        )

def make_request(url, headers, timeout=30, data=None, method="GET"):
    req_obj = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        RedirectHandler(),
        urllib.request.HTTPSHandler(context=ctx)
    )
    try:
        return opener.open(req_obj, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (301,302,303,307,308):
            loc = e.headers.get("Location","")
            if loc:
                if not loc.startswith("http"): loc = urljoin(url, loc)
                return opener.open(
                    urllib.request.Request(loc, data=data, headers=headers, method=method),
                    timeout=timeout
                )
        raise

# ── PW API ────────────────────────────────────────────────────
class PWAPI:
    def __init__(self):
        self.cookies     = {}
        self.user        = None
        self.current_phone = None
        self.batch_cache = {}
        self._lock       = threading.Lock()

    def load_random_session(self):
        all_sessions = [x for x in _load_sessions() if x.get("cookies")]
        if not all_sessions:
            print("⚠️ No sessions with cookies found in DB")
            return False
        for session in random.sample(all_sessions, min(len(all_sessions), 5)):
            self.cookies       = session.get("cookies", {})
            self.user          = session.get("user_data", {})
            self.current_phone = session.get("phone", "?")
            try:
                test = self._json(f"{BASE}/AllBatches?page=1")
                if test.get("success"):
                    print(f"✅ Session loaded: {self.current_phone}")
                    return True
            except Exception: pass
        self.cookies = {}; self.user = None; self.current_phone = None
        return False

    def refresh_session(self):
        s = get_random_session()
        if s:
            with self._lock:
                self.cookies       = s.get("cookies", {})
                self.user          = s.get("user_data", {})
                self.current_phone = s.get("phone")

    def _raw(self, url, method="GET", data=None, extra_headers=None):
        try:
            h = {
                "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept":       "*/*",
                "Content-Type": "application/json",
                "Origin":       "https://pwthor.live",
                "Referer":      "https://pwthor.live/study/batches"
            }
            with self._lock:
                if self.cookies:
                    h["Cookie"] = "; ".join(f"{k}={v}" for k,v in self.cookies.items())
            if extra_headers: h.update(extra_headers)
            payload = json.dumps(data).encode() if data else None
            resp    = make_request(url, h, 30, payload, method)
            sc = resp.getheader("Set-Cookie")
            if sc:
                skip = {"Path","Domain","Expires","Max-Age","HttpOnly","Secure","SameSite"}
                for part in sc.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1); k = k.strip()
                        if k not in skip:
                            with self._lock: self.cookies[k] = v.strip()
            raw = resp.read()
            if len(raw) > 2 and raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return raw, dict(resp.headers)
        except Exception:
            return None, {}

    def _json(self, url, method="GET", data=None, extra_headers=None):
        raw, _ = self._raw(url, method, data, extra_headers)
        if not raw or raw.strip().startswith(b"<"):
            return {"success": False}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {"success": False}

    def _resolve_subject_id(self, batch_id, slug):
        if batch_id not in self.batch_cache:
            r = self._json(f"{BASE}/BatchInfo?BatchId={batch_id}&Type=details")
            if r.get("success"): self.batch_cache[batch_id] = r
        bi = self.batch_cache.get(batch_id, {})
        if bi.get("success"):
            for s in bi.get("data",{}).get("subjects",[]):
                if s.get("slug") == slug:
                    return s.get("subjectId", slug)
        return slug

    def _extract_start_time_text(self, txt):
        if not txt: return ""
        for pat in [r'"startTime":"([^"]+)"', r'"classStartTime":"([^"]+)"',
                    r'"lectureStartTime":"([^"]+)"', r'"liveStartTime":"([^"]+)"']:
            m = re.search(pat, txt)
            if m: return m.group(1).strip()
        return ""

    def _get_live_page_start_time(self, batch_id, sub_id, content_id, known_start_time=""):
        try:
            rsc = "".join(random.choices(string.ascii_letters + string.digits, k=16))
            url = (f"https://pwthor.live/live?batchId={batch_id}&SubjectId={sub_id}"
                   f"&ChildId={content_id}&Type=awsVideo&_rsc={rsc}")
            if known_start_time:
                url += f"&startTime={urllib.parse.quote(known_start_time, safe=':.-TZ+')}"
            raw, _ = self._raw(url, extra_headers={
                "Rsc":      "1",
                "Accept":   "text/x-component, text/plain;q=0.9,*/*;q=0.8",
                "Next-Url": f"/study/batches/{batch_id}",
                "Referer":  f"https://pwthor.live/study/batches/{batch_id}"
            })
            return self._extract_start_time_text((raw or b"").decode("utf-8", errors="ignore"))
        except Exception:
            return ""

    def get_video_info(self, batch_id, content_id, subject_slug):
        # ✅ Cache check
        cache_key = f"{batch_id}:{content_id}:{subject_slug}"
        cached = VIDEO_INFO_CACHE.get(cache_key)
        if cached:
            return cached

        self.refresh_session()
        sub_id = self._resolve_subject_id(batch_id, subject_slug)

        # ✅ Parallel fetch: Schedule + Watch page simultaneously
        def fetch_schedule():
            return self._json(f"{BASE}/Schedule?BatchId={batch_id}&SubjectId={sub_id}&ContentId={content_id}")

        def fetch_watch():
            rsc = "".join(random.choices(string.ascii_letters + string.digits, k=16))
            raw, _ = self._raw(
                f"https://pwthor.live/watch?batchId={batch_id}&SubjectId={sub_id}&ChildId={content_id}"
                f"&Type=penpencilvdo&VideoUrl=&isLocked=true&_rsc={rsc}",
                extra_headers={"Rsc":"1","Accept":"text/html,application/xhtml+xml,*/*"}
            )
            return (raw or b"").decode("utf-8", errors="ignore")

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_sch   = pool.submit(fetch_schedule)
            f_watch = pool.submit(fetch_watch)
            sch        = f_sch.result()
            watch_html = f_watch.result()

        if not sch.get("data"):
            return {"success": False, "error": "Schedule not found"}
        sd = sch["data"]

        tokens = {}
        for name, pat in [
            ("secureToken", r'"secureToken":"([^"]+)"'),
            ("auth_t",      r'"auth_t":"([^"]+)"'),
            ("v",           r'"v":"([^"]+)"')
        ]:
            m = re.search(pat, watch_html)
            if m: tokens[name] = m.group(1)

        dk_m        = re.search(r'"dynamicKey":"([^"]+)"', watch_html)
        dynamic_key = dk_m.group(1) if dk_m else "v"

        if dynamic_key and dynamic_key not in tokens:
            try:
                dyn_m = re.search(rf'"{re.escape(dynamic_key)}":"([^"]+)"', watch_html)
                if dyn_m: tokens[dynamic_key] = dyn_m.group(1)
            except Exception: pass

        if not tokens and sd.get("secureToken"):
            tokens["secureToken"] = sd["secureToken"]
            dynamic_key = sd.get("dynamicKey","v")

        if not tokens:
            return {"success": False, "error": "No token found"}

        ordered = []
        if dynamic_key and dynamic_key in tokens:
            ordered.append((dynamic_key, tokens[dynamic_key]))
        for k, v in tokens.items():
            if k != dynamic_key: ordered.append((k, v))

        mpd_url = signed_url = clear_keys = url_type = video_container = ""
        schedule_info = {}; fallback_data = None

        for _, tv in ordered:
            params = []
            if dynamic_key and dynamic_key not in ("v","auth_t"):
                params.append(f"{dynamic_key}={urllib.parse.quote(tv, safe='')}")
            params += [f"auth_t={tv}", f"v={tv}"]
            for param in params:
                try:
                    vu = self._json(f"{BASE}/get-video-url?{param}")
                    d  = vu.get("data",{})
                    if not d.get("url"): continue
                    if fallback_data is None: fallback_data = d
                    if bool(d.get("clearKeys")) or bool(d.get("hasClearKey")):
                        mpd_url       = d["url"]
                        signed_url    = d.get("signedUrl","")
                        clear_keys    = d.get("clearKeys")
                        video_container = d.get("videoContainer","")
                        url_type      = d.get("urlType","")
                        schedule_info = d.get("scheduleInfo",{}) or {}
                        break
                except Exception: continue
            if mpd_url: break

        if not mpd_url and fallback_data:
            mpd_url         = fallback_data["url"]
            signed_url      = fallback_data.get("signedUrl","")
            clear_keys      = fallback_data.get("clearKeys")
            video_container = fallback_data.get("videoContainer","")
            url_type        = fallback_data.get("urlType","")
            schedule_info   = fallback_data.get("scheduleInfo",{}) or {}

        if not mpd_url:
            return {"success": False, "error": "No MPD URL"}

        _is_hls = mpd_url.endswith(".m3u8") or "m3u8" in mpd_url.lower()

        if _is_hls and url_type == "awsVideo":
            if not schedule_info.get("startTime"):
                ws = self._extract_start_time_text(watch_html)
                if ws: schedule_info["startTime"] = ws
            if not schedule_info.get("startTime"):
                for _k in ("startTime","classStartTime","lectureStartTime","liveStartTime"):
                    _v = sd.get(_k)
                    if isinstance(_v, str) and _v.strip():
                        schedule_info["startTime"] = _v.strip(); break
            if not schedule_info.get("startTime"):
                _st = self._get_live_page_start_time(batch_id, sub_id, content_id, "")
                if _st: schedule_info["startTime"] = _st

        is_live     = _is_hls and bool(schedule_info.get("startTime"))
        p           = urlsplit(mpd_url)
        final_query = p.query

        if signed_url:
            extra = signed_url.lstrip("?&")
            if extra and extra not in final_query:
                final_query = (final_query + "&" + extra) if final_query else extra

        start_param = ""
        if _is_hls and schedule_info.get("startTime"):
            try:
                dt          = datetime.fromisoformat(schedule_info["startTime"].replace("Z","+00:00"))
                epoch       = int(dt.timestamp())
                start_param = f"start={epoch}"
                if start_param not in final_query:
                    final_query = (start_param + "&" + final_query) if final_query else start_param
            except Exception: pass

        # ✅ Normalize: BunnyCDN → CloudFront
        raw_full_url = urlunsplit((p.scheme, p.netloc, p.path, final_query, ""))
        full_url     = normalize_video_url(raw_full_url)

        drm_keys = {}
        if clear_keys:
            if isinstance(clear_keys, dict):
                items = clear_keys.items()
            else:
                items = [
                    (ks.split(":",1)[0], ks.split(":",1)[1])
                    for ks in (clear_keys if isinstance(clear_keys, list) else [])
                    if isinstance(ks, str) and ":" in ks
                ]
            for k, v in items:
                drm_keys[k.strip().replace("-","").lower()] = v.strip().lower()

        kid = next(iter(drm_keys), None)

        result = {
            "success":         True,
            "mpd_url":         full_url,
            "kid":             kid or "",
            "key":             drm_keys.get(kid,"") if kid else "",
            "drm_protected":   bool(drm_keys),
            "is_live":         is_live,
            "url_type":        url_type,
            "start_param":     start_param,
            "video_container": video_container,
            "schedule_info":   schedule_info,
            "topic":           sd.get("topic",""),
            "date":            sd.get("date",""),
            "used_session":    self.current_phone
        }

        # Cache sirf non-live videos karo (live URLs expire hoti hain)
        if not is_live:
            VIDEO_INFO_CACHE.set(cache_key, result)

        return result


api = PWAPI()
if not api.load_random_session():
    print("⚠️ No valid sessions")

# ── SESSION STORE ─────────────────────────────────────────────
SESSIONS      = {}
SESSIONS_LOCK = threading.Lock()

def cleanup_sessions():
    while True:
        try:
            now = time_mod.time()
            with SESSIONS_LOCK:
                old = [sid for sid,v in list(SESSIONS.items())
                       if now - v.get("created",now) > 3600]
                for sid in old:
                    sess = SESSIONS.pop(sid, None)
                    if sess: sess.get("init_cache",{}).clear()
                    MPD_CACHE.delete(f"mpd:{sid}")
                    M3U8_CACHE.delete(f"master:{sid}")
                    M3U8_CACHE.delete(f"audio:{sid}")
        except Exception: pass
        time_mod.sleep(180)

threading.Thread(target=cleanup_sessions, daemon=True).start()

# ── MPD HELPERS ───────────────────────────────────────────────
def rewrite_mpd_for_proxy(mpd_text, manifest_url, session_id=None):
    signed_query = urlsplit(manifest_url).query

    def make_worker_url(raw):
        raw = (raw or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            abs_url = ensure_signed_query(_fix_url(raw), signed_query)
        else:
            abs_url = resolve_url(raw, manifest_url)
            abs_url = ensure_signed_query(_fix_url(abs_url), signed_query)
        return build_worker_cf_url(abs_url, session_id)

    mpd_text = re.sub(r'\s*<BaseURL[^>]*>.*?</BaseURL>\s*', '\n', mpd_text, flags=re.DOTALL)
    mpd_text = re.sub(r'initialization="([^"]+)"',
        lambda m: f'initialization="{make_worker_url(m.group(1))}"', mpd_text)
    mpd_text = re.sub(r'(<SegmentTemplate\b[^>]*?\s)media="([^"]+)"',
        lambda m: f'{m.group(1)}media="{make_worker_url(m.group(2))}"', mpd_text)
    mpd_text = re.sub(r'(<SegmentURL\b[^>]*?\s)media="([^"]+)"',
        lambda m: f'{m.group(1)}media="{make_worker_url(m.group(2))}"', mpd_text)
    return mpd_text

def resolve_url(path, mpd_url):
    if path.startswith("http"):
        p = urlsplit(path); m = urlsplit(mpd_url)
        return f"{path}?{m.query}" if not p.query and m.query else path
    p    = urlsplit(mpd_url)
    base = p.path.rsplit("/",1)[0] + "/"
    full = (f"{p.scheme}://{p.netloc}{path}" if path.startswith("/")
            else f"{p.scheme}://{p.netloc}{base}{path}")
    return full + (f"?{p.query}" if p.query and "?" not in full else "")

def get_ns(root):
    t = root.tag
    return {"mpd": t.split("}")[0].strip("{")} if "{" in t else {}

def _findall(el, tag, ns):
    r = el.findall(f"mpd:{tag}", ns) if ns else []
    if not r: r = el.findall(tag)
    if not r and ns: r = el.findall(f'{{{list(ns.values())[0]}}}{tag}')
    return r

def _find(el, tag, ns):
    r = _findall(el, tag, ns); return r[0] if r else None

def seg_template(tpl, rep_id, bw, num=None, t=None):
    u = tpl.replace("$RepresentationID$", str(rep_id)).replace("$Bandwidth$", str(bw))
    if num is not None:
        u = u.replace("$Number$", str(num))
        u = re.sub(r'\$Number%(\d+)d\$', lambda m: str(num).zfill(int(m.group(1))), u)
    if t is not None: u = u.replace("$Time$", str(t))
    return u

def parse_duration(s):
    if not s or not s.startswith("P"): return 0.0
    m = re.search(r'PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?', s)
    return (float(m.group(1) or 0)*3600 + float(m.group(2) or 0)*60 + float(m.group(3) or 0)
            if m else 0.0)

def dedup_params(url):
    try:
        p = urlsplit(url)
        if not p.query: return url
        params = {k: v[0] for k,v in parse_qs(p.query, keep_blank_values=True).items()}
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(params), ""))
    except Exception: return url

def _guess_init_url(seg_url):
    try:
        p = urlsplit(seg_url); path = p.path
        np = re.sub(r'/\d+\.mp4$', '/init.mp4', path)
        np = re.sub(r'/\d+\.m4s$', '/init.mp4', np)
        if np != path:
            return urlunsplit((p.scheme, p.netloc, np, p.query, ""))
    except Exception: pass
    return ""

# ── HLS HELPERS ───────────────────────────────────────────────
def _extract_signed_params(query):
    if not query: return ""
    try:
        params    = parse_qs(query, keep_blank_values=True)
        important = {k: params[k][0] for k in ["start","Signature","Key-Pair-Id","Policy"]
                     if k in params}
        return urlencode(important) if important else query
    except Exception: return query

def _merge_hls_params(existing, extra):
    if not extra:    return existing
    if not existing: return extra
    try:
        merged = {**parse_qs(extra, keep_blank_values=True),
                  **parse_qs(existing, keep_blank_values=True)}
        return urlencode({k: v[0] for k,v in merged.items()})
    except Exception: return existing

def _build_forced_hls_key_url(manifest_url):
    try:
        p     = urlsplit(manifest_url)
        parts = [x for x in p.path.split("/") if x]
        q     = _extract_signed_params(p.query) or p.query
        if "hls" in parts:
            idx       = parts.index("hls")
            key_parts = parts[:idx+1] + ["enc.key"]
        else:
            key_parts = parts[:-1] + ["hls","enc.key"]
        return urlunsplit((p.scheme, p.netloc, "/"+"/".join(key_parts), q, ""))
    except Exception: return manifest_url
    
    

def _rewrite_hls_manifest(m3u8_text, manifest_url, cf_path="", signed_params="", session_id=None):
    signed_params = _extract_signed_params(urlsplit(manifest_url).query) or signed_params

    def make_abs_url(raw_url):
        raw_url = raw_url.strip()
        if raw_url.startswith("http://") or raw_url.startswith("https://"):
            p = urlsplit(raw_url)
            return urlunsplit((p.scheme, p.netloc, p.path, _merge_hls_params(p.query, signed_params), ""))
        if "?" in raw_url:
            rp, rq = raw_url.split("?",1)
            abs_url = urljoin(manifest_url, rp)
            p = urlsplit(abs_url)
            return urlunsplit((p.scheme, p.netloc, p.path, _merge_hls_params(rq, signed_params), ""))
        return ensure_signed_query(urljoin(manifest_url, raw_url), signed_params)

    def build_segment_worker_url(raw_url, next_raws):
        abs_url = make_abs_url(raw_url)
        out = build_worker_cf_url(abs_url, session_id)

        # next segment hints add karo (relative raw values hi bhejo, short rahenge)
        if next_raws:
            sep = "&" if "?" in out else "?"
            hints = []
            for i, nxt in enumerate(next_raws[:4], 1):
                hints.append(f"__n{i}={quote(nxt.strip(), safe='')}")
            out = out + sep + "&".join(hints)
        return out

    def route_url(raw_url, kind):
        abs_url = make_abs_url(raw_url)
        if kind == "key":
            return build_phone_play_url(abs_url) if "cloudfront.net" in abs_url else abs_url
        if kind == "manifest":
            return build_phone_play_url(abs_url)
        return build_worker_cf_url(abs_url, session_id)

    lines = m3u8_text.splitlines()

    # ✅ first pass: exact segment order collect karo
    ordered_segment_refs = []
    next_kind = None
    for line in lines:
        stripped = line.strip()
        if not stripped:
            next_kind = None
            continue

        if stripped.startswith("#EXTINF"):
            next_kind = "segment"
            continue

        if stripped.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', stripped)
            if m:
                ordered_segment_refs.append(m.group(1))
            next_kind = None
            continue

        if not stripped.startswith("#") and next_kind == "segment":
            ordered_segment_refs.append(stripped)
            next_kind = None
            continue

        next_kind = None

    # ✅ second pass: rewrite with exact next hints
    result = []
    next_kind = None
    seg_idx = 0

    for line in lines:
        stripped = line.strip()

        if not stripped:
            result.append(line)
            next_kind = None
            continue

        if stripped.startswith("#EXT-X-STREAM-INF"):
            result.append(line)
            next_kind = "manifest"
            continue

        if stripped.startswith("#EXTINF"):
            result.append(line)
            next_kind = "segment"
            continue

        if stripped.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                raw_seg = m.group(1)
                next_refs = ordered_segment_refs[seg_idx+1:seg_idx+5]
                new_url = build_segment_worker_url(raw_seg, next_refs)
                result.append(re.sub(r'URI="([^"]+)"', f'URI="{new_url}"', line))
                seg_idx += 1
            else:
                result.append(line)
            next_kind = None
            continue

        if stripped.startswith("#EXT-X-KEY"):
            forced = ""
            try:
                _mq = urlsplit(manifest_url).query
                _mn = urlsplit(manifest_url).netloc
                if "cloudfront.net" in _mn and "start=" not in _mq:
                    forced = build_phone_play_url(_build_forced_hls_key_url(manifest_url))
            except Exception:
                pass
            result.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{forced or route_url(m.group(1),"key")}"', line))
            next_kind = None
            continue

        if not stripped.startswith("#") and next_kind == "manifest":
            result.append(route_url(stripped, "manifest"))
            next_kind = None
            continue

        if not stripped.startswith("#") and next_kind == "segment":
            next_refs = ordered_segment_refs[seg_idx+1:seg_idx+5]
            result.append(build_segment_worker_url(stripped, next_refs))
            seg_idx += 1
            next_kind = None
            continue

        result.append(line)
        next_kind = None

    return "\n".join(result)
    
    
# ── SEGMENT STREAM ────────────────────────────────────────────
def stream_segment_fast(target):
    ck     = hashlib.md5(target.encode()).hexdigest()
    cached = SEGMENT_CACHE.get(ck)
    if cached is not None:
        ct = ("video/MP2T" if target.endswith(".ts") else
              "audio/aac"  if target.endswith(".aac") else "video/mp4")
        return Response(cached, 200, {
            "Content-Type":  ct,
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
            "Content-Length": str(len(cached))
        })
    try:
        r = get_http().get(target, headers=PROXY_H, timeout=25)
        r.raise_for_status()
        data = r.content
        if len(data) < 4*1024*1024:
            SEGMENT_CACHE.set(ck, data)
        return Response(data, 200, {
            "Content-Type":  r.headers.get("Content-Type","application/octet-stream"),
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
            "Content-Length": str(len(data))
        })
    except req_lib.exceptions.HTTPError as e:
        sc = e.response.status_code if e.response else 500
        return Response(f"HTTP {sc}", sc, {"Access-Control-Allow-Origin":"*"})
    except Exception as e:
        return Response(str(e), 500, {"Access-Control-Allow-Origin":"*"})

# ── DOWNLOAD HELPERS ──────────────────────────────────────────
def fetch_mpd_for_download(sid):
    ck     = f"mpd:{sid}"
    cached = MPD_CACHE.get(ck)
    if cached: return cached
    with SESSIONS_LOCK: session = SESSIONS.get(sid)
    if not session: return None
    url = _fix_url(session["mpd_url"])
    for attempt in range(3):
        try:
            r = get_http().get(url, headers=MANIFEST_H, timeout=30)
            r.raise_for_status()
            MPD_CACHE.set(ck, r.text)
            return r.text
        except Exception:
            if attempt < 2: time_mod.sleep(0.5*(attempt+1))
    return None

def seg_proxy_url(full_url, sid):
    return f"/seg/{sid}?url=" + quote(full_url, safe='$~:@!,')

def get_track_id(url):
    path = urlsplit(url).path
    for pat in [r'/dash/([^/]+)/', r'/(\d{3,4}p?|audio|video)/']:
        m = re.search(pat, path)
        if m: return m.group(1)
    parts = path.rsplit("/",2)
    return parts[-2] if len(parts) >= 2 else "default"

def is_init(url):
    return "init" in urlsplit(url).path.rsplit("/",1)[-1].lower()

def mpd_to_master(mpd_xml, sid, host, mpd_url):
    try:
        root  = ET.fromstring(mpd_xml); ns = get_ns(root)
        lines = ["#EXTM3U","#EXT-X-VERSION:3",""]; idx = 0
        for period in (_findall(root,"Period",ns) or [root]):
            for adapt in _findall(period,"AdaptationSet",ns):
                mime = adapt.get("mimeType","") or adapt.get("contentType","")
                for rep in _findall(adapt,"Representation",ns):
                    rm = rep.get("mimeType",mime)
                    if "video" not in rm and not rep.get("height"): continue
                    bw    = rep.get("bandwidth","0")
                    w     = rep.get("width","")
                    h     = rep.get("height","")
                    codec = rep.get("codecs","") or adapt.get("codecs","")
                    rid   = rep.get("id",str(idx))
                    inf   = f"#EXT-X-STREAM-INF:BANDWIDTH={bw}"
                    if w and h: inf += f",RESOLUTION={w}x{h}"
                    if codec:   inf += f',CODECS="{codec}"'
                    lines += [inf, f"{host}/m3u8/{sid}/stream/{rid}.m3u8"]
                    idx += 1
        return "\n".join(lines)
    except Exception: return ""

def mpd_to_stream(mpd_xml, rep_id, sid, mpd_url):
    try:
        root = ET.fromstring(mpd_xml); ns = get_ns(root)
        target_rep = target_adapt = None
        for period in (_findall(root,"Period",ns) or [root]):
            for adapt in _findall(period,"AdaptationSet",ns):
                for rep in _findall(adapt,"Representation",ns):
                    if rep.get("id") == rep_id:
                        target_rep = rep; target_adapt = adapt; break
                if target_rep: break
            if target_rep: break
        if not target_rep: return ""
        rid  = target_rep.get("id",rep_id); bw = target_rep.get("bandwidth","0")
        tmpl = _find(target_rep,"SegmentTemplate",ns) or _find(target_adapt,"SegmentTemplate",ns)
        if not tmpl: return ""
        init_tpl   = tmpl.get("initialization",""); media_tpl = tmpl.get("media","")
        start_num  = int(tmpl.get("startNumber","1")); timescale = int(tmpl.get("timescale","1"))
        dur_attr   = tmpl.get("duration","0")
        lines      = ["#EXTM3U","#EXT-X-VERSION:3","#EXT-X-MEDIA-SEQUENCE:0","#EXT-X-ALLOW-CACHE:YES"]
        if init_tpl:
            lines.append(f'#EXT-X-MAP:URI="{seg_proxy_url(resolve_url(seg_template(init_tpl,rid,bw,start_num),mpd_url),sid)}"')
        timeline = _find(tmpl,"SegmentTimeline",ns)
        if timeline is not None:
            num = cur_t = 0; max_dur = 0
            for s in _findall(timeline,"S",ns):
                t = s.get("t"); d = int(s.get("d",0)); r = int(s.get("r",0))
                if t: cur_t = int(t)
                for _ in range(r+1):
                    ds = d/timescale if timescale else d; max_dur = max(max_dur,ds)
                    lines += [f"#EXTINF:{ds:.3f},",
                              seg_proxy_url(resolve_url(seg_template(media_tpl,rid,bw,num,cur_t),mpd_url),sid)]
                    cur_t += d; num += 1
            lines.insert(4, f"#EXT-X-TARGETDURATION:{int(max_dur)+1}")
        elif dur_attr and media_tpl:
            dv = int(dur_attr); ds = dv/timescale if timescale else dv
            lines.insert(4, f"#EXT-X-TARGETDURATION:{int(ds)+1}")
            total = parse_duration(root.get("mediaPresentationDuration",""))
            count = int(total/ds)+2 if total and ds else 300
            for i in range(count):
                lines += [f"#EXTINF:{ds:.3f},",
                          seg_proxy_url(resolve_url(seg_template(media_tpl,rid,bw,start_num+i),mpd_url),sid)]
        lines.append("#EXT-X-ENDLIST"); return "\n".join(lines)
    except Exception: return ""

def mpd_to_audio_stream(mpd_xml, sid, mpd_url):
    try:
        root = ET.fromstring(mpd_xml); ns = get_ns(root)
        audio_rep = audio_adapt = None
        for period in (_findall(root,"Period",ns) or [root]):
            for adapt in _findall(period,"AdaptationSet",ns):
                if "audio" in (adapt.get("mimeType","") or adapt.get("contentType","")).lower():
                    reps = _findall(adapt,"Representation",ns)
                    if reps: audio_rep = reps[0]; audio_adapt = adapt; break
            if audio_rep: break
        if not audio_rep: return ""
        rid  = audio_rep.get("id","0"); bw = audio_rep.get("bandwidth","0")
        tmpl = _find(audio_rep,"SegmentTemplate",ns) or _find(audio_adapt,"SegmentTemplate",ns)
        if not tmpl: return ""
        init_tpl  = tmpl.get("initialization",""); media_tpl = tmpl.get("media","")
        start_num = int(tmpl.get("startNumber","1")); timescale = int(tmpl.get("timescale","1"))
        dur_attr  = tmpl.get("duration","0")
        lines     = ["#EXTM3U","#EXT-X-VERSION:3","#EXT-X-MEDIA-SEQUENCE:0","#EXT-X-ALLOW-CACHE:YES"]
        if init_tpl:
            lines.append(f'#EXT-X-MAP:URI="{seg_proxy_url(resolve_url(seg_template(init_tpl,rid,bw,start_num),mpd_url),sid)}"')
        timeline = _find(tmpl,"SegmentTimeline",ns)
        if timeline is not None:
            num = cur_t = 0; max_dur = 0
            for s in _findall(timeline,"S",ns):
                t = s.get("t"); d = int(s.get("d",0)); r = int(s.get("r",0))
                if t: cur_t = int(t)
                for _ in range(r+1):
                    ds = d/timescale if timescale else d; max_dur = max(max_dur,ds)
                    lines += [f"#EXTINF:{ds:.3f},",
                              seg_proxy_url(resolve_url(seg_template(media_tpl,rid,bw,num,cur_t),mpd_url),sid)]
                    cur_t += d; num += 1
            lines.insert(4, f"#EXT-X-TARGETDURATION:{int(max_dur)+1}")
        elif dur_attr and media_tpl:
            dv = int(dur_attr); ds = dv/timescale if timescale else dv
            lines.insert(4, f"#EXT-X-TARGETDURATION:{int(ds)+1}")
            total = parse_duration(root.get("mediaPresentationDuration",""))
            count = int(total/ds)+2 if total and ds else 300
            for i in range(count):
                lines += [f"#EXTINF:{ds:.3f},",
                          seg_proxy_url(resolve_url(seg_template(media_tpl,rid,bw,start_num+i),mpd_url),sid)]
        lines.append("#EXT-X-ENDLIST"); return "\n".join(lines)
    except Exception: return ""

def mpd_to_hls_direct(mpd_xml, rep_id, mpd_url):
    try:
        root = ET.fromstring(mpd_xml); ns = get_ns(root)
        target_rep = target_adapt = None
        for period in (_findall(root,"Period",ns) or [root]):
            for adapt in _findall(period,"AdaptationSet",ns):
                for rep in _findall(adapt,"Representation",ns):
                    if rep.get("id") == rep_id:
                        target_rep = rep; target_adapt = adapt; break
                if target_rep: break
            if target_rep: break
        if not target_rep: return ""
        rid  = target_rep.get("id",rep_id); bw = target_rep.get("bandwidth","0")
        tmpl = _find(target_rep,"SegmentTemplate",ns) or _find(target_adapt,"SegmentTemplate",ns)
        if not tmpl: return ""
        init_tpl  = tmpl.get("initialization",""); media_tpl = tmpl.get("media","")
        start_num = int(tmpl.get("startNumber","1")); timescale = int(tmpl.get("timescale","1"))
        dur_attr  = tmpl.get("duration","0")
        make_w    = lambda seg_path: build_worker_cf_url(resolve_url(seg_path, mpd_url))
        lines     = ["#EXTM3U","#EXT-X-VERSION:6","#EXT-X-INDEPENDENT-SEGMENTS","#EXT-X-MEDIA-SEQUENCE:0"]
        if init_tpl:
            lines.append(f'#EXT-X-MAP:URI="{make_w(seg_template(init_tpl,rid,bw,start_num))}"')
        timeline = _find(tmpl,"SegmentTimeline",ns)
        if timeline is not None:
            num = cur_t = 0; max_dur = 0
            for s in _findall(timeline,"S",ns):
                t = s.get("t"); d = int(s.get("d",0)); r = int(s.get("r",0))
                if t: cur_t = int(t)
                for _ in range(r+1):
                    ds = d/timescale if timescale else d; max_dur = max(max_dur,ds)
                    lines += [f"#EXTINF:{ds:.3f},", make_w(seg_template(media_tpl,rid,bw,num,cur_t))]
                    cur_t += d; num += 1
            lines.insert(4, f"#EXT-X-TARGETDURATION:{int(max_dur)+1}")
        elif dur_attr and media_tpl:
            dv = int(dur_attr); ds = dv/timescale if timescale else dv
            lines.insert(4, f"#EXT-X-TARGETDURATION:{int(ds)+1}")
            total = parse_duration(root.get("mediaPresentationDuration",""))
            count = int(total/ds)+5 if total and ds else 300
            for i in range(count):
                lines += [f"#EXTINF:{ds:.3f},", make_w(seg_template(media_tpl,rid,bw,start_num+i))]
        lines.append("#EXT-X-ENDLIST"); return "\n".join(lines)
    except Exception: return ""

# ── DECRYPTION ────────────────────────────────────────────────
def is_mp4decrypt_available():
    try: subprocess.run(["mp4decrypt"], capture_output=True, timeout=5); return True
    except FileNotFoundError: return False
    except Exception: return True

MP4DECRYPT_AVAILABLE = is_mp4decrypt_available()

def _decrypt(data, kid, key, init_data=None):
    if not kid or not key or not MP4DECRYPT_AVAILABLE: return data
    with _DECRYPT_SEM:
        combined = (init_data or b"") + data; in_p = out_p = None
        try:
            in_fd, in_p = tempfile.mkstemp(suffix=".mp4"); out_p = in_p + ".dec"
            os.write(in_fd, combined); os.close(in_fd)
            r = subprocess.run(["mp4decrypt","--key",f"{kid}:{key}",in_p,out_p],
                               capture_output=True, timeout=30)
            if r.returncode != 0 or not os.path.exists(out_p): return data
            with open(out_p,"rb") as f: result = f.read()
            if not result: return data
            if init_data:
                idx = result.find(b"moof")
                if idx > 4:                          return result[idx-4:]
                elif len(result) > len(init_data):   return result[len(init_data):]
            return result
        except Exception: return data
        finally:
            for _p in [in_p, out_p]:
                if _p and os.path.exists(_p):
                    try: os.unlink(_p)
                    except Exception: pass

# ── ROUTES ────────────────────────────────────────────────────
CORS_H = {
    "Access-Control-Allow-Origin":  "*",
    "Access-Control-Allow-Methods": "*",
    "Access-Control-Allow-Headers": "*"
}

@app.route("/api/prepare", methods=["POST"])
def fn_prepare():
    try:
        body         = request.get_json(force=True)
        video_id     = body.get("videoId","").strip()
        subject_slug = body.get("subjectSlug","").strip()
        batch_id     = body.get("batchId","").strip() or DEFAULT_BATCH_ID

        if not video_id or not subject_slug:
            return jsonify({"success":False,"error":"videoId + subjectSlug required"}), 400
        if count_active_sessions() == 0:
            return jsonify({"success":False,"error":"No active sessions"}), 401

        info = None
        for attempt in range(3):
            try:
                info = api.get_video_info(batch_id, video_id, subject_slug)
                if info.get("success"): break
            except Exception:
                if attempt < 2: time_mod.sleep(1); api.refresh_session()

        if not info or not info.get("success"):
            return jsonify({"success":False,"error":(info or {}).get("error","Failed")}), 500

        host  = request.headers.get("X-Forwarded-Host") or request.host
        proto = request.headers.get("X-Forwarded-Proto") or ("https" if request.is_secure else "http")
        base  = f"{proto}://{host}"

        # ✅ normalize_video_url already called inside get_video_info
        # BunnyCDN → CF conversion already done
        original_url  = info["mpd_url"]
        is_live       = info.get("is_live", False)
        schedule_info = dict(info.get("schedule_info") or {})

        # ── LIVE ─────────────────────────────────────────────
        _is_live_class = False
        if original_url and "m3u8" in original_url.lower():
            _url_path = urlsplit(original_url).path.lower()
            _has_uuid = bool(re.search(r'/[0-9a-f]{8}-[0-9a-f]{4}-', _url_path))
            if (is_live
                    or ("index.m3u8" in _url_path and not _has_uuid)
                    or (info.get("url_type") == "awsVideo" and not _has_uuid)):
                _is_live_class = True

        if _is_live_class:
            final_url  = original_url
            start_time = (schedule_info.get("startTime") or "").strip()
            if not start_time:
                try:
                    sub_id     = api._resolve_subject_id(batch_id, subject_slug)
                    start_time = api._get_live_page_start_time(batch_id, sub_id, video_id, "")
                    if start_time: schedule_info["startTime"] = start_time
                except Exception: pass
            if "start=" not in final_url:
                try:
                    epoch = 0
                    if start_time:
                        dt    = datetime.fromisoformat(start_time.replace("Z","+00:00"))
                        epoch = int(dt.timestamp())
                    else:
                        epoch = int(info.get("start_param","").replace("start=","") or 0)
                    if epoch:
                        sep       = "&" if "?" in final_url else "?"
                        final_url = f"{final_url}{sep}start={epoch}"
                except Exception: pass
            return jsonify({
                "success":True,"is_live":True,"session_id":"",
                "manifest_url":final_url,"live_url":final_url,
                "hls_url":final_url,"m3u8_url":final_url,
                "kid":info.get("kid",""),"key":info.get("key",""),
                "drm_protected":info.get("drm_protected",False),
                "video_container":"HLS","schedule_info":schedule_info,
                "topic":info.get("topic",""),"date":info.get("date",""),
                "pw_session_used":info.get("used_session","")
            })

        # ── Recorded HLS ─────────────────────────────────────
        if original_url and "m3u8" in original_url.lower():
            final_url = original_url
            if "start=" not in final_url:
                try:
                    epoch = int(info.get("start_param","").replace("start=","") or 0)
                    if epoch:
                        sep       = "&" if "?" in final_url else "?"
                        final_url = f"{final_url}{sep}start={epoch}"
                except Exception: pass
            hls_sid = "hls-" + secrets.token_urlsafe(6)
            proxied = build_proxy_manifest_url(base, final_url, hls_sid)
            return jsonify({
                "success":True,"is_live":False,"session_id":"",
                "manifest_url":proxied,"live_url":proxied,
                "hls_url":proxied,"m3u8_url":proxied,
                "kid":info.get("kid",""),"key":info.get("key",""),
                "drm_protected":info.get("drm_protected",False),
                "video_container":"HLS","schedule_info":schedule_info,
                "topic":info.get("topic",""),"date":info.get("date",""),
                "pw_session_used":info.get("used_session","")
            })

        # ── Recorded DASH ─────────────────────────────────────
        norm_url     = extract_cloudfront_signed_url(original_url)
        manifest_url = build_proxy_manifest_url(base, norm_url)
        sid          = secrets.token_urlsafe(10)

        with SESSIONS_LOCK:
            SESSIONS[sid] = {
                "mpd_url":    norm_url,
                "kid":        info.get("kid","").replace("-","").lower(),
                "key":        info.get("key","").lower(),
                "init_cache": {},
                "batch_id":   batch_id,
                "created":    time_mod.time(),
                "play_closed":False
            }

        # ✅ Probe native HLS in background - don't block response
        native = probe_native_hls(norm_url)
        hls_m  = (build_proxy_manifest_url(base, native) if native
                  else f"{base}/hls/{sid}/master.m3u8")

        return jsonify({
            "success":True,"is_live":False,"session_id":sid,
            "manifest_url":manifest_url,"m3u8_url":hls_m,
            "hls_url":hls_m,"live_url":"",
            "kid":info.get("kid",""),"key":info.get("key",""),
            "drm_protected":info.get("drm_protected",False),
            "video_container":info.get("video_container","DASH"),
            "schedule_info":{},"topic":info.get("topic",""),
            "date":info.get("date",""),
            "pw_session_used":info.get("used_session","")
        })

    except Exception as e:
        return jsonify({"success":False,"error":str(e)}), 500


@app.route("/play/<path:cf_path>", methods=["GET","HEAD","OPTIONS"])
def fn_play(cf_path):
    if request.method == "OPTIONS":
        return Response("", headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*"
        })

    query = request.query_string.decode("utf-8")
    target = _fix_url(f"https://{cf_path}" + (f"?{query}" if query else ""))

    if request.method == "HEAD":
        try:
            r = get_http().head(target, headers=PROXY_H, timeout=10, allow_redirects=True)
            return Response("", status=r.status_code, headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Type": r.headers.get("Content-Type", "application/octet-stream"),
                "Content-Length": r.headers.get("Content-Length", ""),
            })
        except Exception:
            return Response("", 200, {"Access-Control-Allow-Origin": "*"})

    if not ENABLE_PLAY:
        return jsonify({"success": False, "error": PLAY_OFF_MSG}), 503

    try:
        r = get_http().get(target, headers=MANIFEST_H, timeout=30)

        if r.status_code == 403:
            return Response("Signature expired", 410, {
                "Access-Control-Allow-Origin": "*",
                "X-Signature-Expired": "true"
            })

        r.raise_for_status()

        ct = r.headers.get("Content-Type", "application/octet-stream")
        if cf_path.endswith(".mpd"):
            ct = "application/dash+xml; charset=utf-8"
        elif cf_path.endswith(".m3u8"):
            ct = "application/x-mpegURL; charset=utf-8"

        cache_ctrl = "public, max-age=86400"
        if cf_path.endswith(".mpd") or cf_path.endswith(".m3u8"):
            cache_ctrl = "public, max-age=60"

        return Response(r.content, r.status_code, {
            "Content-Type": ct,
            "Content-Length": str(len(r.content)),
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Accept-Ranges": "bytes",
            "Cache-Control": cache_ctrl,
        })

    except req_lib.exceptions.HTTPError as e:
        sc = e.response.status_code if e.response else 500
        return Response(f"HTTP {sc}", sc, {"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return Response(str(e), 500, {"Access-Control-Allow-Origin": "*"})

# ── DOWNLOAD ROUTES ───────────────────────────────────────────
@app.route('/m3u8/<sid>/master.m3u8', methods=['GET','OPTIONS'])
def fn_master(sid):
    if request.method=='OPTIONS': return Response('',headers=CORS_H)
    if not ENABLE_DOWNLOAD: return jsonify({"success":False,"error":DOWNLOAD_OFF_MSG}),503
    with SESSIONS_LOCK: session=SESSIONS.get(sid)
    if not session: return Response('Session not found',404,{"Access-Control-Allow-Origin":"*"})
    ck=f"master:{sid}"; cached=M3U8_CACHE.get(ck)
    if cached: return Response(cached,200,{'Content-Type':'application/x-mpegURL','Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=600'})
    try:
        xml=fetch_mpd_for_download(sid)
        if not xml: return Response('MPD fetch failed',502,{"Access-Control-Allow-Origin":"*"})
        host=request.headers.get('X-Forwarded-Host') or request.host
        proto=request.headers.get('X-Forwarded-Proto') or ('https' if request.is_secure else 'http')
        content=mpd_to_master(xml,sid,f"{proto}://{host}",_fix_url(session['mpd_url']))
        if not content: return Response('Conversion failed',500,{"Access-Control-Allow-Origin":"*"})
        M3U8_CACHE.set(ck,content)
        return Response(content,200,{'Content-Type':'application/x-mpegURL','Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=600'})
    except Exception as e: return Response(str(e),500,{"Access-Control-Allow-Origin":"*"})

@app.route('/m3u8/<sid>/stream/<rep_id>.m3u8', methods=['GET','OPTIONS'])
def fn_stream(sid,rep_id):
    if request.method=='OPTIONS': return Response('',headers=CORS_H)
    if not ENABLE_DOWNLOAD: return jsonify({"success":False,"error":DOWNLOAD_OFF_MSG}),503
    with SESSIONS_LOCK: session=SESSIONS.get(sid)
    if not session: return Response('Session not found',404,{"Access-Control-Allow-Origin":"*"})
    ck=f"stream:{sid}:{rep_id}"; cached=M3U8_CACHE.get(ck)
    if cached: return Response(cached,200,{'Content-Type':'application/x-mpegURL','Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=600'})
    try:
        xml=fetch_mpd_for_download(sid)
        if not xml: return Response('MPD fetch failed',502,{"Access-Control-Allow-Origin":"*"})
        content=mpd_to_stream(xml,rep_id,sid,_fix_url(session['mpd_url']))
        if not content: return Response(f'Rep {rep_id} not found',404,{"Access-Control-Allow-Origin":"*"})
        M3U8_CACHE.set(ck,content)
        return Response(content,200,{'Content-Type':'application/x-mpegURL','Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=600'})
    except Exception as e: return Response(str(e),500,{"Access-Control-Allow-Origin":"*"})

@app.route('/m3u8/<sid>/audio.m3u8', methods=['GET','OPTIONS'])
def fn_audio(sid):
    if request.method=='OPTIONS': return Response('',headers=CORS_H)
    if not ENABLE_DOWNLOAD: return jsonify({"success":False,"error":DOWNLOAD_OFF_MSG}),503
    with SESSIONS_LOCK: session=SESSIONS.get(sid)
    if not session: return Response('Session not found',404,{"Access-Control-Allow-Origin":"*"})
    ck=f"audio:{sid}"; cached=M3U8_CACHE.get(ck)
    if cached: return Response(cached,200,{'Content-Type':'application/x-mpegURL','Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=600'})
    try:
        xml=fetch_mpd_for_download(sid)
        if not xml: return Response('MPD fetch failed',502,{"Access-Control-Allow-Origin":"*"})
        content=mpd_to_audio_stream(xml,sid,_fix_url(session['mpd_url']))
        if not content: return Response('No audio track found',404,{"Access-Control-Allow-Origin":"*"})
        M3U8_CACHE.set(ck,content)
        return Response(content,200,{'Content-Type':'application/x-mpegURL','Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=600'})
    except Exception as e: return Response(str(e),500,{"Access-Control-Allow-Origin":"*"})

@app.route('/seg/<sid>', methods=['GET','HEAD','OPTIONS'])
def fn_seg(sid):
    if request.method=='OPTIONS':
        return Response('',headers={**CORS_H,"Access-Control-Allow-Methods":"GET, HEAD, OPTIONS"})
    if not ENABLE_DOWNLOAD:
        return jsonify({"success":False,"error":DOWNLOAD_OFF_MSG}),503
    with SESSIONS_LOCK: session=SESSIONS.get(sid)
    if not session: return Response('Session stopped',410,{"Access-Control-Allow-Origin":"*"})
    if request.method=='HEAD':
        return Response('',200,{'Access-Control-Allow-Origin':'*','Content-Type':'video/mp4','Accept-Ranges':'bytes'})
    target=urllib.parse.unquote(request.args.get('url',''))
    while '%25' in target: target=urllib.parse.unquote(target)
    target=_fix_url(target)
    if not target: return Response('url required',400,{"Access-Control-Allow-Origin":"*"})
    if '$' in target: return Response('Unreplaced template',400,{"Access-Control-Allow-Origin":"*"})
    mpd_url=_fix_url(session['mpd_url'])
    if not target.startswith('http'): target=resolve_url(target,mpd_url)
    pt=urlsplit(target)
    if not pt.query:
        pm=urlsplit(mpd_url)
        if pm.query: target+=f"?{pm.query}"
    target=dedup_params(target); track=get_track_id(target); init_seg=is_init(target)
    ck=hashlib.md5(f"{sid}:{target}".encode()).hexdigest()
    cached=DECRYPT_CACHE.get(ck)
    if cached:
        return Response(cached,200,{'Access-Control-Allow-Origin':'*','Content-Type':'video/mp4',
                                    'Content-Length':str(len(cached)),'Accept-Ranges':'bytes'})
    with SESSIONS_LOCK:
        if sid not in SESSIONS:
            return Response('Session stopped',410,{"Access-Control-Allow-Origin":"*"})
    try:
        r=get_http().get(target,headers=PROXY_H,timeout=60); r.raise_for_status(); raw=r.content
        kid=session.get('kid',''); key=session.get('key','')
        if init_seg:
            session['init_cache'][track]=raw; result=_decrypt(raw,kid,key)
            DECRYPT_CACHE.set(ck,result)
            return Response(result,200,{'Access-Control-Allow-Origin':'*','Content-Type':'video/mp4',
                                        'Content-Length':str(len(result))})
        else:
            init_data=session['init_cache'].get(track)
            if init_data is None:
                iu=_guess_init_url(target)
                if iu:
                    try:
                        ir=get_http().get(iu,headers=PROXY_H,timeout=30)
                        if ir.status_code==200:
                            init_data=ir.content; session['init_cache'][track]=init_data
                    except Exception: pass
            result=_decrypt(raw,kid,key,init_data); DECRYPT_CACHE.set(ck,result)
            return Response(result,200,{'Access-Control-Allow-Origin':'*','Content-Type':'video/mp4',
                                        'Content-Length':str(len(result))})
    except req_lib.exceptions.HTTPError as e:
        sc=e.response.status_code if e.response else 500
        return Response(f'HTTP {sc}',sc,{"Access-Control-Allow-Origin":"*"})
    except Exception as e:
        return Response(str(e),500,{"Access-Control-Allow-Origin":"*"})

@app.route('/hls/<sid>/master.m3u8', methods=['GET','OPTIONS'])
def fn_hls_master(sid):
    if request.method=='OPTIONS': return Response('',headers=CORS_H)
    with SESSIONS_LOCK: session=SESSIONS.get(sid)
    if not session: return Response('Session not found',404,{"Access-Control-Allow-Origin":"*"})
    try:
        xml=fetch_mpd_for_download(sid)
        if not xml: return Response('MPD fetch failed',502,{"Access-Control-Allow-Origin":"*"})
        host=request.headers.get('X-Forwarded-Host') or request.host
        proto=request.headers.get('X-Forwarded-Proto') or ('https' if request.is_secure else 'http')
        content=mpd_to_master(xml,sid,f"{proto}://{host}",_fix_url(session['mpd_url'])).replace('/m3u8/','/hls/')
        return Response(content,200,{'Content-Type':'application/x-mpegURL','Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=600'})
    except Exception as e: return Response(str(e),500,{"Access-Control-Allow-Origin":"*"})

@app.route('/hls/<sid>/stream/<rep_id>.m3u8', methods=['GET','OPTIONS'])
def fn_hls_stream(sid,rep_id):
    if request.method=='OPTIONS': return Response('',headers=CORS_H)
    with SESSIONS_LOCK: session=SESSIONS.get(sid)
    if not session: return Response('Session not found',404,{"Access-Control-Allow-Origin":"*"})
    try:
        xml=fetch_mpd_for_download(sid)
        if not xml: return Response('MPD fetch failed',502,{"Access-Control-Allow-Origin":"*"})
        content=mpd_to_hls_direct(xml,rep_id,_fix_url(session['mpd_url']))
        if not content: return Response(f'Rep {rep_id} not found',404,{"Access-Control-Allow-Origin":"*"})
        return Response(content,200,{'Content-Type':'application/x-mpegURL','Access-Control-Allow-Origin':'*','Cache-Control':'public, max-age=600'})
    except Exception as e: return Response(str(e),500,{"Access-Control-Allow-Origin":"*"})

# ── KHAZANA ───────────────────────────────────────────────────
@app.route("/api/khazana-prepare", methods=["POST","OPTIONS"])
def fn_khazana_prepare():
    if request.method=="OPTIONS": return Response("",headers=CORS_H)
    try:
        body       = request.get_json(force=True)
        program_id = body.get("programId","").strip()
        child_id   = body.get("childId","").strip()
        video_id   = body.get("videoId","").strip()
        if not program_id or not child_id or not video_id:
            return jsonify({"success":False,"error":"programId, childId, videoId required"}),400
        if count_active_sessions()==0:
            return jsonify({"success":False,"error":"No active sessions"}),401

        api.refresh_session()
        rsc = "".join(random.choices(string.ascii_letters+string.digits,k=16))
        watch_raw,_ = api._raw(
            f"https://pwthor.live/watch?ChildId={child_id}&Type=LECTURE&programId={program_id}&videoId={video_id}&_rsc={rsc}",
            extra_headers={"Rsc":"1","Accept":"text/x-component, text/plain;q=0.9,*/*;q=0.8"}
        )
        watch_html = (watch_raw or b"").decode("utf-8",errors="ignore")

        st_m         = re.search(r'"secureToken":"([^"]+)"',watch_html)
        secure_token = st_m.group(1) if st_m else ""
        dk_m         = re.search(r'"dynamicKey":"([^"]+)"',watch_html)
        dynamic_key  = dk_m.group(1) if dk_m else "auth_t"

        if not secure_token:
            return jsonify({"success":False,"error":"No secureToken found"}),404

        params_to_try = []
        if dynamic_key not in ("v","auth_t"):
            params_to_try.append(f"{dynamic_key}={urllib.parse.quote(secure_token,safe='')}")
        params_to_try += [
            f"auth_t={urllib.parse.quote(secure_token,safe='')}",
            f"v={urllib.parse.quote(secure_token,safe='')}"
        ]

        mpd_url = signed_url = clear_keys = None; fallback = None
        for param in params_to_try:
            try:
                vu = api._json(f"{BASE}/get-video-url?{param}")
                d  = vu.get("data",{})
                if not d.get("url"): continue
                if fallback is None: fallback = d
                if bool(d.get("clearKeys")) or bool(d.get("hasClearKey")):
                    mpd_url = d["url"]; signed_url = d.get("signedUrl","")
                    clear_keys = d.get("clearKeys"); break
            except Exception: continue

        if not mpd_url and fallback:
            mpd_url    = fallback["url"]
            signed_url = fallback.get("signedUrl","")
            clear_keys = fallback.get("clearKeys")
        if not mpd_url:
            return jsonify({"success":False,"error":"No video URL"}),404

        p  = urlsplit(mpd_url); fq = p.query
        if signed_url:
            extra = signed_url.lstrip("?&")
            if extra and extra not in fq:
                fq = (fq+"&"+extra) if fq else extra

        # ✅ normalize: BunnyCDN → CF
        raw_full = urlunsplit((p.scheme,p.netloc,p.path,fq,""))
        full_url = normalize_video_url(raw_full)

        drm_keys = {}
        if clear_keys:
            if isinstance(clear_keys,dict):
                items = clear_keys.items()
            else:
                items = [(ks.split(":",1)[0],ks.split(":",1)[1])
                         for ks in (clear_keys if isinstance(clear_keys,list) else [])
                         if isinstance(ks,str) and ":" in ks]
            for k,v in items:
                drm_keys[k.strip().replace("-","").lower()] = v.strip().lower()

        kid     = next(iter(drm_keys),None)
        key_val = drm_keys.get(kid,"") if kid else ""
        host    = request.headers.get("X-Forwarded-Host") or request.host
        proto   = request.headers.get("X-Forwarded-Proto") or ("https" if request.is_secure else "http")
        base    = f"{proto}://{host}"

        if full_url.endswith(".m3u8") or "m3u8" in full_url.lower():
            pm = build_proxy_manifest_url(base,full_url)
            return jsonify({"success":True,"session_id":"","manifest_url":pm,
                            "hls_url":pm,"m3u8_url":pm,"kid":kid or "","key":key_val or "",
                            "drm_protected":bool(drm_keys)})

        manifest_url = build_proxy_manifest_url(base,full_url)
        sid          = secrets.token_urlsafe(10)
        with SESSIONS_LOCK:
            SESSIONS[sid] = {
                "mpd_url":    full_url,
                "kid":        (kid or "").replace("-","").lower(),
                "key":        (key_val or "").lower(),
                "init_cache": {},
                "batch_id":   "khazana",
                "created":    time_mod.time(),
                "play_closed":False
            }
        native = probe_native_hls(full_url)
        hls_m  = build_proxy_manifest_url(base,native) if native else f"{base}/hls/{sid}/master.m3u8"
        return jsonify({
            "success":True,"session_id":sid,"manifest_url":manifest_url,
            "m3u8_url":hls_m,"hls_url":hls_m,"kid":kid or "","key":key_val or "",
            "drm_protected":bool(drm_keys)
        })

    except Exception as e:
        return jsonify({"success":False,"error":str(e)}),500

# ── UTILS ─────────────────────────────────────────────────────
@app.route("/api/session/<sid>/stop", methods=["POST","OPTIONS"])
def fn_stop_session(sid):
    if request.method=="OPTIONS": return Response("",headers=CORS_H)
    with SESSIONS_LOCK:
        if sid in SESSIONS: SESSIONS[sid]["play_closed"] = True
    return Response("",200,{"Access-Control-Allow-Origin":"*"})

@app.route("/health")
def fn_health():
    return jsonify({
        "status":           "ok",
        "proxy_sessions":   len(SESSIONS),
        "pw_sessions":      count_active_sessions(),
        "mongodb":          sessions_col is not None,
        "segment_cache":    SEGMENT_CACHE.stats(),
        "decrypt_cache":    DECRYPT_CACHE.stats(),
        "video_info_cache": VIDEO_INFO_CACHE.cache.__len__(),
        "mp4decrypt":       MP4DECRYPT_AVAILABLE,
        "play_enabled":     ENABLE_PLAY,
        "download_enabled": ENABLE_DOWNLOAD
    })

@app.route("/config", methods=["POST"])
def fn_config():
    global ENABLE_PLAY, ENABLE_DOWNLOAD
    body = request.get_json(force=True)
    if "play"     in body: ENABLE_PLAY     = bool(body["play"])
    if "download" in body: ENABLE_DOWNLOAD = bool(body["download"])
    return jsonify({"success":True,"play_enabled":ENABLE_PLAY,"download_enabled":ENABLE_DOWNLOAD})

@app.route("/")
def fn_root():
    return jsonify({"service":"PW Proxy","status":"ok"})

# ── MAIN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"PW Proxy | port={port} | sessions={count_active_sessions()} | mp4decrypt={'✅' if MP4DECRYPT_AVAILABLE else '❌'}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
