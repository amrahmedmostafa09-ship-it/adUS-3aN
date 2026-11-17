# -*- coding: utf-8 -*-
"""Qureo Solver Tool — خادم ويب آمن متعدد الجلسات.

كل زائر يحصل على جلسة عشوائية مستقلة وWorker منفصل، لذلك لا تتشارك عمليات
الحل حالة RUN_STATE أو STOP_EVENT أو بيانات الدخول. كلمة المرور تبقى في ذاكرة
العامل فقط، ولا تُكتب في ملف أو تُرسل إلى المتصفح أو السجل.
"""
import argparse
import contextlib
import io
import json
import multiprocessing as mp
import os
import re
import secrets
import socket
import threading
import time
from argparse import Namespace
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from multiprocessing import Queue
from pathlib import Path
from queue import Empty
from urllib.parse import parse_qs, urlparse

import requests

import financial_track
import main as core

UI_PATH = Path(__file__).resolve().parent / "web" / "index.html"
ASSET_DIR = Path(__file__).resolve().parent / "web" / "assets"
ASSET_TYPES = {".otf": "font/otf", ".ttf": "font/ttf", ".png": "image/png", ".woff2": "font/woff2"}
YEARS = {"first": ("javascript",), "second": ("javascript", "python", "financial")}
AVAILABLE_TRACKS = {"javascript", "python", "financial"}
MAX_LINES = 3000
MAX_BODY = 10_000
MAX_SESSIONS = 128
SESSION_COOKIE = "qureo_sid"
SESSION_TTL = 24 * 60 * 60
MODES = ("preview", "submit", "retake", "resume")
_TAG = re.compile(r"^\[([A-Za-z_\-]+)\]")
_CHAPTER = re.compile(r"^\[CHAPTER\]\s+([^|]+)")
_MP = mp.get_context("fork") if "fork" in mp.get_all_start_methods() else mp.get_context()
LEVEL_LIMITS = {"javascript": 54, "python": 25, "financial": financial_track.MAX_FIN_LEVEL}
JS_LIMIT_BY_YEAR = {"first": 42, "second": 54}


def level_limit(year, track):
    return JS_LIMIT_BY_YEAR[year] if track == "javascript" else LEVEL_LIMITS[track]


def safe_client_ip(handler):
    forwarded = handler.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    return forwarded or handler.client_address[0]


_IP_CACHE = {}
_IP_CACHE_LOCK = threading.Lock()


def egypt_country_for(ip):
    """Return EG/other, أو None للاتصالات المحلية، مع Cache قصير."""
    try:
        parsed = ip_address(ip)
        if parsed.is_private or parsed.is_loopback or parsed.is_reserved:
            return None, None
    except ValueError:
        return None, "عنوان اتصال غير صالح"
    token = os.environ.get("IPINFO_TOKEN", "").strip()
    if not token:
        return None, None
    now = time.time()
    with _IP_CACHE_LOCK:
        cached = _IP_CACHE.get(ip)
        if cached and cached[0] > now:
            return cached[1], None
    try:
        response = requests.get(
            f"https://ipinfo.io/{ip}/json",
            params={"token": token},
            timeout=3,
            headers={"Accept": "application/json", "User-Agent": "QureoSolver/2.0"},
        )
        response.raise_for_status()
        country = str(response.json().get("country") or "").upper() or None
    except (requests.RequestException, ValueError, TypeError):
        return None, "تعذر التحقق من دولة الاتصال عبر IPInfo"
    with _IP_CACHE_LOCK:
        _IP_CACHE[ip] = (now + 600, country)
    return country, None


class LogBuffer(io.TextIOBase):
    def __init__(self, secrets_to_hide=()):
        self._lock = threading.Lock()
        self._lines, self._partial, self._base = [], "", 0
        self._hide = [str(s) for s in secrets_to_hide if s and len(str(s)) >= 3]
        self.counts = {}

    def writable(self):
        return True

    def _clean(self, text):
        for secret in self._hide:
            text = text.replace(secret, "••••")
        return text

    def write(self, text):
        if not text:
            return 0
        with self._lock:
            self._partial += text
            while "\n" in self._partial:
                line, self._partial = self._partial.split("\n", 1)
                self._push(line)
        return len(text)

    def _push(self, line):
        line = self._clean(line.rstrip("\r"))
        if not line.strip():
            return
        match = _TAG.match(line.strip())
        tag = match.group(1) if match else ""
        if tag:
            self.counts[tag] = self.counts.get(tag, 0) + 1
        self._lines.append({"i": self._base + len(self._lines), "t": time.strftime("%H:%M:%S"), "tag": tag, "text": line})
        if len(self._lines) > MAX_LINES:
            drop = len(self._lines) - MAX_LINES
            del self._lines[:drop]
            self._base += drop

    def flush_partial(self):
        with self._lock:
            if self._partial.strip():
                self._push(self._partial)
            self._partial = ""

    def since(self, index):
        with self._lock:
            end = self._base + len(self._lines)
            start = max(index, self._base)
            return list(self._lines[start - self._base:]), end


class QueueStdout(io.TextIOBase):
    def __init__(self, queue, secrets_to_hide=()):
        self.queue, self.partial = queue, ""
        self.hide = [str(s) for s in secrets_to_hide if s and len(str(s)) >= 3]

    def writable(self):
        return True

    def write(self, text):
        self.partial += text or ""
        while "\n" in self.partial:
            line, self.partial = self.partial.split("\n", 1)
            for secret in self.hide:
                line = line.replace(secret, "••••")
            self.queue.put({"type": "line", "line": line})
        return len(text or "")

    def flush(self):
        if self.partial.strip():
            for secret in self.hide:
                self.partial = self.partial.replace(secret, "••••")
            self.queue.put({"type": "line", "line": self.partial})
        self.partial = ""


def worker_main(args_data, creds, track, event_queue, stop_event):
    """عامل مستقل: لا يشارك أي حالة Python مع جلسات المستخدمين الأخرى."""
    core.STOP_EVENT = stop_event
    stdout = QueueStdout(event_queue, creds)
    code = 1
    try:
        args = Namespace(**args_data)
        with contextlib.redirect_stdout(stdout):
            if track == "financial":
                code = financial_track.run(args.to_level, creds, stop_event, preview_only=args.preview_only)
            else:
                code = core.run(args, credentials=creds)
    except BaseException as exc:
        stdout.write(f"[ERROR] {exc}\n")
    finally:
        stdout.flush()
        results = list(core.RUN_STATE.get("results") or [])
        try:
            event_queue.put({"type": "done", "code": code, "stopped": stop_event.is_set(), "results": results})
        except (BrokenPipeError, EOFError, OSError):
            pass


class Job:
    def __init__(self, client_ip=""):
        self.lock = threading.RLock()
        self.state = "idle"
        self.log = LogBuffer()
        self.mode = None
        self.year, self.track = None, None
        self.levels = (1, 42)
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.results = []
        self.current = None
        self.client_ip = client_ip
        self.process = None
        self.queue = None
        self.stop_event = None
        self.last_used = time.time()

    def start(self, payload):
        mode = payload.get("mode", "submit")
        if mode not in MODES:
            return 400, {"error": "وضع غير معروف."}
        year, track = payload.get("year", "first"), payload.get("track", "javascript")
        if year not in YEARS or track not in YEARS[year] or track not in AVAILABLE_TRACKS:
            return 400, {"error": "المهمة دي مش متاحة لسنتك الدراسية."}
        limit = level_limit(year, track)
        try:
            lo = 1 if track == "financial" else int(payload.get("from_level", 1))
            hi = int(payload.get("to_level", limit))
        except (TypeError, ValueError):
            return 400, {"error": "أرقام المستويات غير صحيحة."}
        if not (1 <= lo <= hi <= limit):
            return 400, {"error": f"المستويات بين 1 و{limit} والبداية لا تتجاوز النهاية."}
        if track == "financial":
            problem = financial_track.check_available()
            if problem:
                return 400, {"error": problem}
        login_id, password = payload.get("student_id"), payload.get("password")
        if not all(isinstance(v, str) and v.strip() for v in (login_id, password)) or len(login_id) > 200 or len(password) > 200:
            return 400, {"error": "أدخل Login ID وكلمة المرور."}
        if mode != "preview" and payload.get("confirm") is not True:
            return 400, {"error": "لازم تأكيد إرسال الإجابات قبل البدء."}
        country, geo_error = egypt_country_for(self.client_ip)
        if geo_error:
            return 503, {"error": geo_error + ". حاول مرة أخرى بعد قليل."}
        if country and country != "EG":
            return 403, {"error": "الأداة متاحة حاليًا للمستخدمين داخل مصر فقط."}
        with self.lock:
            self.last_used = time.time()
            if self.state in ("running", "stopping"):
                return 409, {"error": "فيه تشغيل شغّال في جلستك الآن."}
            self.state = "running"
            self.mode, self.levels, self.year, self.track = mode, (lo, hi), year, track
            self.started_at, self.finished_at, self.exit_code = time.time(), None, None
            self.results, self.current = [], None
            self.log = LogBuffer([login_id.strip(), password])
            self.queue = _MP.Queue()
            self.stop_event = _MP.Event()
            args_data = {"track": track, "from_level": lo, "to_level": hi, "preview_only": mode == "preview",
                         "retake": mode in ("retake", "resume"), "resume_current": mode == "resume"}
            self.process = _MP.Process(target=worker_main, args=(args_data, (login_id.strip(), password), track,
                                                                  self.queue, self.stop_event), daemon=True)
            self.process.start()
            threading.Thread(target=self._pump, daemon=True).start()
        return 200, {"ok": True}

    def _pump(self):
        queue = self.queue
        process = self.process
        assert queue is not None and process is not None
        finished = False
        while True:
            try:
                item = queue.get(timeout=0.25)
            except Empty:
                if not process.is_alive():
                    break
                continue
            if item.get("type") == "line":
                line = str(item.get("line") or "")
                match = _CHAPTER.match(line.strip())
                if match:
                    with self.lock:
                        self.current = match.group(1).strip()
                self.log.write(line + "\n")
            elif item.get("type") == "done":
                with self.lock:
                    self.results = item.get("results") if isinstance(item.get("results"), list) else []
                    self.exit_code = item.get("code")
                    self.finished_at = time.time()
                    self.state = "stopped" if item.get("stopped") else "finished"
                    self.current = None
                    finished = True
                break
        self.log.flush_partial()
        if not finished:
            with self.lock:
                self.exit_code = self.exit_code if self.exit_code is not None else 1
                self.finished_at = time.time()
                self.state = "stopped" if self.stop_event and self.stop_event.is_set() else "finished"
                self.log.write("[ERROR] انتهى عامل التشغيل قبل إرسال نتيجة نهائية.\n")
        process.join(timeout=1)

    def stop(self):
        with self.lock:
            self.last_used = time.time()
            if self.state != "running":
                return 409, {"error": "مفيش تشغيل شغّال في جلستك."}
            self.state = "stopping"
            if self.stop_event:
                self.stop_event.set()
        return 200, {"ok": True}

    def status(self, since=0):
        with self.lock:
            self.last_used = time.time()
            state, mode, year, track = self.state, self.mode, self.year, self.track
            levels, started, finished_at, exit_code = self.levels, self.started_at, self.finished_at, self.exit_code
            results, current = list(self.results), self.current
        lines, nxt = self.log.since(since)
        incomplete = dict(core.incomplete_chapters(results))
        order, final = [], {}
        for row in results:
            cid = str(row.get("chapter_id"))
            if cid not in final:
                order.append(cid)
            final[cid] = True
        chapters = [{"id": cid, "status": "incomplete" if cid in incomplete else "done", "reason": incomplete.get(cid)} for cid in order]
        if state in ("running", "stopping") and current is not None and str(current) not in final:
            chapters.append({"id": str(current), "status": "running", "reason": None})
        now = finished_at or time.time()
        return {"state": state, "mode": mode, "year": year, "track": track, "from_level": levels[0], "to_level": levels[1],
                "elapsed": int(now - started) if started else 0, "exit_code": exit_code,
                "current_chapter": str(current) if current is not None else None, "lines": lines, "next": nxt,
                "chapters": chapters,
                "stats": {"chapters_done": sum(1 for c in chapters if c["status"] == "done"),
                          "chapters_incomplete": len(incomplete), "answers_sent": self.log.counts.get("ANSWER-SENT", 0)}}


class SessionStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}

    def get_or_create(self, client_ip):
        with self.lock:
            if len(self.sessions) >= MAX_SESSIONS:
                finished = [item for item in self.sessions.items() if item[1].state not in ("running", "stopping")]
                if finished:
                    old_sid, _ = min(finished, key=lambda item: item[1].last_used)
                    del self.sessions[old_sid]
                else:
                    raise RuntimeError("الخادم ممتلئ مؤقتًا؛ أعد المحاولة بعد قليل.")
            sid = secrets.token_urlsafe(32)
            self.sessions[sid] = Job(client_ip)
            return sid, self.sessions[sid]

    def get(self, sid):
        with self.lock:
            return self.sessions.get(sid)


class Handler(BaseHTTPRequestHandler):
    server_version = "QureoSolver/2.0"
    store: "SessionStore"

    def log_message(self, format, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8", cache="no-store", extra_headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _cookie_sid(self):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return ""
        morsel = jar.get(SESSION_COOKIE)
        return morsel.value if morsel is not None else ""

    def _session(self, create=False):
        store = self.store
        assert store is not None
        sid = self._cookie_sid()
        job = store.get(sid) if sid else None
        if job is not None:
            return sid, job, False
        if not create:
            return "", None, False
        try:
            new_sid, new_job = store.get_or_create(safe_client_ip(self))
            return new_sid, new_job, True
        except RuntimeError as exc:
            self._send(503, {"error": str(exc)})
            return "", None, False

    @staticmethod
    def _set_cookie(sid):
        return f"{SESSION_COOKIE}={sid}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; Secure; SameSite=None"

    def _asset(self, name):
        target = ASSET_DIR / name
        if "/" in name or "\\" in name or name.startswith(".") or target.suffix not in ASSET_TYPES or not target.is_file():
            return self._send(404, {"error": "not found"})
        return self._send(200, target.read_bytes(), ASSET_TYPES[target.suffix], cache="public, max-age=86400")

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/health":
            return self._send(200, {"ok": True, "service": "qureo-solver"})
        if url.path == "/manus-routes.json":
            route_file = Path(__file__).resolve().parent / "public" / "manus-routes.json"
            return self._send(200, route_file.read_bytes(), "application/json; charset=utf-8", cache="no-cache")
        if url.path.startswith("/assets/"):
            return self._asset(url.path[len("/assets/"):])
        if url.path in ("/", "/index.html"):
            sid, job, created = self._session(create=True)
            if job is None:
                return None
            headers = {"Set-Cookie": self._set_cookie(sid)} if created else None
            return self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8", cache="no-cache", extra_headers=headers)
        if url.path == "/api/status":
            sid, job, _ = self._session(create=False)
            if job is None:
                return self._send(401, {"error": "انتهت جلسة المتصفح؛ أعد تحميل الصفحة."})
            try:
                since = int((query.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            return self._send(200, job.status(since))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        sid, job, _ = self._session(create=False)
        if job is None:
            return self._send(401, {"error": "انتهت جلسة المتصفح؛ أعد تحميل الصفحة."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, {"error": "الطلب كبير جدًا."})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "JSON غير صالح."})
        if url.path == "/api/start":
            code, body = job.start(payload)
        elif url.path == "/api/stop":
            code, body = job.stop()
        else:
            code, body = 404, {"error": "not found"}
        return self._send(code, body)


def make_server(host="0.0.0.0", port=3000):
    store = SessionStore()
    handler = type("BoundHandler", (Handler,), {"store": store})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="Qureo Solver Tool — الواجهة")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3000")))
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print("=== Qureo Solver Tool ===")
    print(f"استمع الخادم على 0.0.0.0:{args.port} — جلسة مستقلة لكل متصفح")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nتم الإغلاق.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
