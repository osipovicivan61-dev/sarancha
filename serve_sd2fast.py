import json
import os
import random
import re
import secrets
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8765"))
SITE = "http://43.173.73.4:3000"
TMP_RPC = "http://papi2.temp-mail.org/rpc/"
TM_API = "https://api.mail.tm"
UGUU = "https://uguu.se/upload.php"
KEY_RE = re.compile(r"^sk-[A-Za-z0-9_-]{16,}$")
MAX_KEYS = 5000

JOBS = {}
JOBS_LOCK = threading.Lock()
KEYS = set()
KEYS_LOCK = threading.Lock()
KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys.json")


def load_keys():
    try:
        with open(KEYS_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            KEYS.update(x for x in data if isinstance(x, str) and x.startswith("sk-"))
    except Exception:
        pass


def save_keys():
    try:
        with open(KEYS_FILE, "w") as f:
            json.dump(sorted(KEYS), f)
    except Exception:
        pass


def new_key():
    with KEYS_LOCK:
        if len(KEYS) >= MAX_KEYS:
            raise RuntimeError("key limit")
        k = "sk-" + secrets.token_hex(20)
        KEYS.add(k)
        save_keys()
    return k


def valid_key(k):
    if not k or not KEY_RE.match(k):
        return False
    with KEYS_LOCK:
        return k in KEYS


def rnd(n):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def http(op, url, data=None, headers=None, timeout=90, retries=4):
    import http.client
    hdrs = {"Connection": "close"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=hdrs)
        op2 = urllib.request.build_opener() if op is None else op
        try:
            with op2.open(req, timeout=timeout) as r:
                try:
                    body = r.read()
                except http.client.IncompleteRead as e:
                    body = e.partial
                return getattr(r, "status", r.getcode()), body
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            except Exception:
                return e.code, b""
        except http.client.IncompleteRead as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2)
                continue
            if getattr(e, "partial", None):
                return 200, e.partial
            raise
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1)
                continue
            raise
    raise last


class _NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def site_opener():
    cj = CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op._cj = cj
    return op


def no_redir_opener(op):
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(op._cj), _NoRedir)


def rpc(method, params):
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    st, body = http(None, TMP_RPC, data=payload, headers={"Content-Type": "application/json"})
    return json.loads(body)


def mailbox_tmp():
    ots = rpc("user.create_ots", {})
    res = rpc("user.get_sid", {"ots": (ots.get("result") or {}).get("ots")})
    sid = (res.get("result") or {}).get("sid")
    if not sid:
        raise RuntimeError("no sid")
    doms = rpc("getdomains", {"sid": sid})
    r = doms.get("result")
    lst = r.get("domains") if isinstance(r, dict) else r
    domain = lst[0] if isinstance(lst, list) and lst else "temp-mail.org"
    addr = rnd(10) + "@" + domain
    rpc("mailbox.new", {"sid": sid, "email": addr})
    return addr


def mailbox_tm():
    st, body = http(None, TM_API + "/domains")
    domain = json.loads(body)["hydra:member"][0]["domain"]
    addr = rnd(10) + "@" + domain
    pw = rnd(12) + "A1"
    http(None, TM_API + "/accounts",
         data=json.dumps({"address": addr, "password": pw}).encode(),
         headers={"Content-Type": "application/json"})
    return addr


def open_session(addr, password):
    op = site_opener()
    name = "u" + rnd(6)
    st, _ = http(op, SITE + "/api/auth/register",
                 data=json.dumps({"name": name, "email": addr, "password": password}).encode(),
                 headers={"Content-Type": "application/json"})
    if st != 201:
        raise RuntimeError("register " + str(st))
    st, body = http(op, SITE + "/api/auth/csrf")
    token = json.loads(body).get("csrfToken")
    if not token:
        raise RuntimeError("csrf")
    form = urllib.parse.urlencode(
        {"csrfToken": token, "email": addr, "password": password, "json": "true"}).encode()
    st, _ = http(no_redir_opener(op), SITE + "/api/auth/callback/credentials", data=form,
                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    if st >= 400:
        raise RuntimeError("login " + str(st))
    st, body = http(op, SITE + "/api/user")
    user = json.loads(body).get("user")
    if not user:
        raise RuntimeError("session")
    return op


def run_job(job_id, params):
    job = JOBS[job_id]
    step = "start"
    try:
        with JOBS_LOCK:
            job["state"] = "running"
        step = "mailbox"
        try:
            addr = mailbox_tmp()
        except Exception:
            addr = mailbox_tm()
        step = "session"
        op = open_session(addr, "Qq" + rnd(10) + "1!")
        model = params.get("model")
        if model not in ("fast", "sd2"):
            model = "fast" if params.get("fast", True) else "sd2"
        is_fast = model == "fast"
        refs = params.get("refs") or []
        has_refs = len(refs) > 0
        if has_refs:
            generation_mode = "multimodal"
            is_fast = False
        else:
            generation_mode = "text_to_video"
        dur = max(4, min(15, int(params.get("duration", 4))))
        if is_fast and dur > 12:
            dur = 12
        step = "create"
        body = {
            "model": "seedance2",
            "prompt": params.get("prompt", ""),
            "params": {
                "duration": dur,
                "aspectRatio": params.get("ratio", "16:9"),
                "resolution": "720p",
                "generateAudio": True,
            },
            "isFast": is_fast,
            "generationMode": generation_mode,
        }
        if has_refs:
            body["referenceUrls"] = refs
        st, resp = http(op, SITE + "/api/generations",
                        data=json.dumps(body).encode(),
                        headers={"Content-Type": "application/json"})
        if st != 201:
            raise RuntimeError("gen " + str(st))
        gen = json.loads(resp).get("generation") or {}
        gid = gen.get("id")
        if not gid:
            raise RuntimeError("gen id")
        deadline = time.time() + 20 * 60
        step = "poll"
        while time.time() < deadline:
            time.sleep(4)
            try:
                st, resp = http(op, SITE + "/api/generations/%s/status" % gid)
                s = (json.loads(resp).get("status") or "").lower()
            except Exception:
                time.sleep(4)
                continue
            if s == "completed":
                step = "result"
                st, resp = http(op, SITE + "/api/generations/%s" % gid)
                url = (json.loads(resp).get("generation") or {}).get("resultUrl")
                if not url:
                    raise RuntimeError("no url")
                with JOBS_LOCK:
                    job["url"] = url
                    job["state"] = "done"
                return
            if s == "failed":
                err = ""
                try:
                    st2, resp2 = http(op, SITE + "/api/generations/%s" % gid)
                    err = (json.loads(resp2).get("generation") or {}).get("errorMessage") or ""
                except Exception:
                    pass
                if not err:
                    try:
                        err = (json.loads(resp).get("errorMessage") or "")
                    except Exception:
                        pass
                raise RuntimeError("failed" + ((": " + err[:200]) if err else ""))
        raise RuntimeError("timeout")
    except Exception as e:
        with JOBS_LOCK:
            job["state"] = "error"
            job["message"] = step + ": " + str(e)[:280]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        if raw is None:
            raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sd2fast.html"), "rb") as f:
                self._send(200, raw=f.read(), ctype="text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            qs = urllib.parse.parse_qs(parsed.query)
            jid = (qs.get("id") or [""])[0]
            job = JOBS.get(jid)
            if not job:
                self._send(404, {"error": "unknown"})
                return
            with JOBS_LOCK:
                self._send(200, {"state": job["state"], "url": job.get("url"),
                                 "message": job.get("message")})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/keys":
            try:
                k = new_key()
            except Exception as e:
                self._send(429, {"error": str(e)})
                return
            self._send(200, {"key": k})
            return
        if path == "/api/run":
            try:
                params = json.loads(body or b"{}")
            except Exception:
                self._send(400, {"error": "bad json"})
                return
            key = (params.get("key") or "").strip() or (self.headers.get("X-Api-Key") or "").strip()
            if not valid_key(key):
                self._send(403, {"error": "invalid key"})
                return
            if not (params.get("prompt") or "").strip():
                self._send(400, {"error": "empty prompt"})
                return
            model = params.get("model")
            if model not in ("fast", "sd2"):
                model = "fast" if params.get("fast", True) else "sd2"
            params["model"] = model
            jid = rnd(12)
            with JOBS_LOCK:
                JOBS[jid] = {"state": "pending"}
            threading.Thread(target=run_job, args=(jid, params), daemon=True).start()
            self._send(200, {"job": jid})
            return
        if path == "/proxy/uguu/" or path == "/api/upload":
            ct = self.headers.get("Content-Type") or "application/octet-stream"
            try:
                st, resp = http(None, UGUU, data=body, headers={"Content-Type": ct}, timeout=180)
                self._send(st, raw=resp)
            except Exception as e:
                self._send(502, {"error": str(e)[:120]})
            return
        self._send(404, {"error": "not found"})


load_keys()

if __name__ == "__main__":
    print("listen on 0.0.0.0:%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
