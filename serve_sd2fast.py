import json
import os
import random
import re
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

JOBS = {}
JOBS_LOCK = threading.Lock()


def rnd(n):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def http(op, url, data=None, headers=None, timeout=90, retries=3):
    hdrs = {"Connection": "close"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=hdrs)
        if op is None:
            op2 = urllib.request.build_opener()
        else:
            op2 = op
        try:
            with op2.open(req, timeout=timeout) as r:
                return getattr(r, "status", r.getcode()), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2)
                continue
            raise
    raise last


def site_opener():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


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
    st, _ = http(op, SITE + "/api/auth/callback/credentials", data=form,
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
    try:
        with JOBS_LOCK:
            job["state"] = "running"
        try:
            addr = mailbox_tmp()
        except Exception:
            addr = mailbox_tm()
        op = open_session(addr, "Qq" + rnd(10) + "1!")
        body = {
            "model": "seedance2",
            "prompt": params.get("prompt", ""),
            "params": {
                "duration": max(4, min(15, int(params.get("duration", 4)))),
                "ratio": params.get("ratio", "16:9"),
            },
            "isFast": bool(params.get("fast", True)),
            "generationMode": "text_to_video",
        }
        refs = params.get("refs") or []
        if refs:
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
        while time.time() < deadline:
            time.sleep(4)
            st, resp = http(op, SITE + "/api/generations/%s/status" % gid)
            s = (json.loads(resp).get("status") or "").lower()
            if s == "completed":
                st, resp = http(op, SITE + "/api/generations/%s" % gid)
                url = (json.loads(resp).get("generation") or {}).get("resultUrl")
                if not url:
                    raise RuntimeError("no url")
                with JOBS_LOCK:
                    job["url"] = url
                    job["state"] = "done"
                return
            if s == "failed":
                raise RuntimeError("failed")
        raise RuntimeError("timeout")
    except Exception as e:
        with JOBS_LOCK:
            job["state"] = "error"
            job["message"] = str(e)[:300]


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
        if path == "/api/run":
            try:
                params = json.loads(body or b"{}")
            except Exception:
                self._send(400, {"error": "bad json"})
                return
            if not KEY_RE.match((params.get("key") or "").strip()):
                self._send(403, {"error": "bad key"})
                return
            if not (params.get("prompt") or "").strip():
                self._send(400, {"error": "empty prompt"})
                return
            jid = rnd(12)
            with JOBS_LOCK:
                JOBS[jid] = {"state": "pending"}
            threading.Thread(target=run_job, args=(jid, params), daemon=True).start()
            self._send(200, {"job": jid})
            return
        if path == "/proxy/uguu/":
            ct = self.headers.get("Content-Type") or "application/octet-stream"
            try:
                st, resp = http(None, UUGUU, data=body, headers={"Content-Type": ct}, timeout=180)
                self._send(st, raw=resp)
            except Exception as e:
                self._send(502, {"error": str(e)[:120]})
            return
        self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print("listen on 0.0.0.0:%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
