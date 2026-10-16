"""SD 2 Fast — локальный сервер-прокси.

Браузер не может ходить напрямую (нет CORS), поэтому:
  /              — страница sd2fast.html
  /proxy/seed/*  — http://43.173.73.4:3000/*   (сайт генерации, куки держит сервер)
  /proxy/tmp/*   — https://papi2.temp-mail.org/rpc/  (temp-mail)
  /proxy/tm/*    — https://api.mail.tm/*       (запасной почтовый сервис)
Вся логика — в HTML, тут только туннель.
"""
from __future__ import annotations

import http.cookiejar
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import os
PORT = int(os.environ.get("PORT", "8765"))
SEED = "http://43.173.73.4:3000"
TMP = "https://papi2.temp-mail.org/rpc/"
TM = "https://api.mail.tm"
UGUU = "https://uguu.se/upload.php"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
HTML = Path(__file__).with_name("sd2fast.html")

# куки сессии сайта генерации (один локальный пользователь)
_seed_jar = http.cookiejar.CookieJar()


class _NoRedir(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_seed_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_seed_jar), _NoRedir)


def _seed_headers(extra: dict | None = None) -> dict:
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _out(self, status: int, body: bytes, ctype: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        if self.path in ("/", "/index.html", "/sd2fast.html"):
            self._out(200, HTML.read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/proxy/seed/"):
            self._seed("GET")
        elif self.path.startswith("/proxy/tm/"):
            self._tm("GET")
        else:
            self._out(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.startswith("/proxy/seed/"):
            self._seed("POST")
        elif self.path.startswith("/proxy/tmp/"):
            self._tmp()
        elif self.path.startswith("/proxy/tm/"):
            self._tm("POST")
        elif self.path.startswith("/proxy/uguu/"):
            self._uguu()
        else:
            self._out(404, b"not found", "text/plain")

    # ── загрузка файлов на uguu.se (референсы) ──────────────
    def _uguu(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type") or ""
        req = urllib.request.Request(UGUU, data=body, method="POST", headers={
            "User-Agent": UA, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                self._out(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            self._out(e.code, e.read()[:1000])
        except Exception as e:
            self._out(502, str(e).encode())

    # ── сайт генерации ──────────────────────────────────────
    def _seed(self, method: str):
        sub = unquote(self.path[len("/proxy/seed/"):])
        url = SEED + ("/" + sub if not sub.startswith("/") else sub)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        ctype = self.headers.get("Content-Type") or "application/json"
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=_seed_headers({"Content-Type": ctype}))
        try:
            with _seed_opener.open(req, timeout=120) as resp:
                self._out(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:  # логин: редирект = успех, куки уже встали
                self._out(200, b'{"ok":true}')
            else:
                self._out(e.code, e.read()[:2000])
        except Exception as e:
            self._out(502, str(e).encode())

    # ── temp-mail rpc ───────────────────────────────────────
    def _tmp(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        req = urllib.request.Request(TMP, data=body, method="POST", headers={
            "User-Agent": UA, "Content-Type": "application/json",
            "Referer": "https://temp-mail.org/ru/", "Origin": "https://temp-mail.org"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._out(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            self._out(e.code, e.read()[:1000])
        except Exception as e:
            self._out(502, str(e).encode())

    # ── mail.tm (запасной) ──────────────────────────────────
    def _tm(self, method: str):
        sub = unquote(self.path[len("/proxy/tm/"):])
        url = TM + ("/" + sub if not sub.startswith("/") else sub)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        auth = self.headers.get("Authorization")
        h = {"User-Agent": UA}
        if body:
            h["Content-Type"] = "application/json"
        if auth:
            h["Authorization"] = auth
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._out(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            self._out(e.code, e.read()[:1000])
        except Exception as e:
            self._out(502, str(e).encode())

    def do_PUT(self):
        if self.path.startswith("/proxy/seed/"):
            self._seed("PUT")
        else:
            self._out(404, b"not found", "text/plain")


if __name__ == "__main__":
    print(f"SD 2 Fast: http://127.0.0.1:{PORT}/")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
