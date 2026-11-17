#!/usr/bin/env python3
# MOODY QUREO Requests Assistant — lessons only, safe assessment mode
import argparse
import getpass
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests
import assessment_runner

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
API_BASE = "https://me-tp.qureo.education"
PORTAL_URL = "https://me-portal.qureo.education"
COURSE_CODE = "javascript_ar"
SECTION_ID = 5
# المسارات التي تمر عبر بوابة كيريو: (كود الكورس، رقم السكشن). بايثون: python_short_ar / 106 (25 فصل).
TRACKS = {"javascript": ("javascript_ar", 5), "python": ("python_short_ar", 106)}
DEFAULT_PAUSE = 0.35
TIMEOUT = 30


def save_json(name, obj):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / name
    target.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_excerpt(response):
    """Return a short error body without echoing credential-like values."""
    text = response.text[:500]
    text = re.sub(r'(?i)(authorization|access_token|refresh_token|id_token|password|cookie)("?\s*[:=]\s*"?)[^",\s}]+', r'\1\2[REDACTED]', text)
    return text


def response_snapshot(response):
    """Safe, small response metadata for login diagnostics (never URLs' queries or cookies)."""
    if response is None:
        return None

    def location(value):
        parts = urlsplit(str(value or ""))
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"
        return parts.path or "/"

    return {
        "status": getattr(response, "status_code", None),
        "content_type": (getattr(response, "headers", {}) or {}).get("Content-Type", ""),
        "final_url": location(getattr(response, "url", "")),
        "redirects": [
            {"status": item.status_code, "url": location(getattr(item, "url", ""))}
            for item in (getattr(response, "history", None) or [])
        ],
    }


def is_cloudfront_country_block(response):
    """Detect Qureo's explicit CloudFront country-block response without logging its body."""
    if response is None or getattr(response, "status_code", None) != 403:
        return False
    headers = {str(k).lower(): str(v).lower() for k, v in (getattr(response, "headers", {}) or {}).items()}
    body = (getattr(response, "text", "") or "")[:2000].lower()
    return ("cloudfront" in headers.get("server", "") or "cloudfront" in headers.get("via", "")) and (
        "block access from your country" in body or "configured to block access" in body
    )


def save_login_diagnostic(**responses):
    """Write only non-secret request facts that help diagnose the portal-to-study hand-off."""
    save_json("login_diagnostic.json", {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "This file intentionally contains no password, cookie, token, or response body.",
        "responses": {name: response_snapshot(response) for name, response in responses.items()},
    })


def is_html_response(response, body):
    content_type = ((getattr(response, "headers", {}) or {}).get("Content-Type", "")).lower()
    if "text/html" in content_type:
        return True
    return isinstance(body, str) and bool(re.search(r"<!doctype\\s+html|<html[\\s>]", body[:500], re.I))


def is_verified_student(data):
    """Accept only an actual study-account object, not a truthy portal/error JSON object."""
    if not isinstance(data, dict):
        return False
    candidates = [data, data.get("student"), data.get("user")]
    for item in candidates:
        if isinstance(item, dict) and any(item.get(key) not in (None, "")
                                          for key in ("id", "student_id", "user_id", "uuid")):
            return True
    return False


STOP_EVENT = threading.Event()          # set by the web UI «إيقاف» button
RUN_STATE = {"chapter": None, "results": []}   # read by the web UI while a run is active


def prompt_credentials():
    print("\n=== MOODY QUREO | تسجيل الدخول ===")
    login_id = input("Login ID: ").strip()
    password = getpass.getpass("Password: ")
    if not login_id or not password:
        raise RuntimeError("لازم تدخل Login ID وكلمة المرور.")
    return login_id, password


def login(session, credentials=None):
    login_id, password = credentials if credentials else prompt_credentials()
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
    session.headers.update({"User-Agent": ua, "Accept-Language": "ar,en-US;q=0.8,en;q=0.7"})
    landing = session.get(PORTAL_URL + "/", timeout=TIMEOUT)
    landing.raise_for_status()
    password_login = session.post(
        PORTAL_URL + "/oauth2/password-login",
        json={"student_id": login_id, "password": password},
        headers={"Origin": PORTAL_URL, "Referer": PORTAL_URL + "/", "Content-Type": "application/json"},
        allow_redirects=False,
        timeout=TIMEOUT,
    )
    if password_login.status_code not in (200, 201, 202, 204, 302, 303, 307, 308):
        save_login_diagnostic(password_login=password_login)
        raise RuntimeError(f"فشل تسجيل الدخول: HTTP {password_login.status_code}: {safe_excerpt(password_login)}")

    # The browser first enters the study host via this course URL.  This is an
    # important SSO hand-off: a successful portal response alone does not mean
    # the separate study service has an active student session.
    study_landing = None
    student_me = None
    try:
        study_landing = session.get(
            API_BASE + "/?content_id=" + COURSE_CODE,
            headers={"Accept": "text/html,application/xhtml+xml", "Referer": PORTAL_URL + "/"},
            timeout=TIMEOUT,
        )
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Referer": API_BASE + "/course/" + COURSE_CODE}
        student_me = session.get(API_BASE + "/api/study/students/me", headers=headers, timeout=TIMEOUT)
        if student_me.status_code == 200:
            try:
                data = student_me.json()
            except ValueError:
                data = None
            if not is_html_response(student_me, data) and is_verified_student(data):
                print("[LOGIN] تم تأكيد جلسة الطالب من خادم الدراسة.")
                return data
        if is_cloudfront_country_block(study_landing) or is_cloudfront_country_block(student_me):
            save_login_diagnostic(password_login=password_login, study_landing=study_landing, student_me=student_me)
            raise RuntimeError(
                "خادم QUREO يحظر منطقة استضافة الموقع (CloudFront HTTP 403). "
                "لم تُرسل أي إجابة؛ يلزم تشغيل الـBackend داخل مصر أو السماح بعنوان الخادم من QUREO."
            )
    except requests.RequestException:
        pass

    save_login_diagnostic(password_login=password_login, study_landing=study_landing, student_me=student_me)
    details = ""
    if student_me is not None:
        details = f" (حالة التحقق: HTTP {student_me.status_code})"
    raise RuntimeError(
        "لم يتم تفعيل جلسة الدراسة بعد تسجيل الدخول" + details +
        ". لم تُرسل أي إجابة. أرسل data/login_diagnostic.json فقط للمراجعة؛ لا ترسل Cookies أو Tokens."
    )


def redact(value, key=""):
    """Redact secrets recursively before writing diagnostic traffic."""
    secret = re.compile(r"token|authorization|cookie|password|secret|student_id|email", re.I)
    if secret.search(str(key)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)bearer\s+[a-z0-9._~-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)(access_token|refresh_token|id_token|authorization|cookie)(\s*[:=]\s*)[^,;\s]+", r"\1\2[REDACTED]", value)
    return value


def audit_api(method, path, payload, response, body):
    """Record QUREO API behavior for debugging; never records auth endpoints or headers."""
    if path.startswith("/oauth2/") or path.endswith("/students/me"):
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / "api_traffic_audit.jsonl"
    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": method,
        "path": path,
        "request": redact(payload),
        "status": response.status_code,
        "response": redact(body),
    }
    # Bound log size to avoid uncontrolled growth.
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if target.exists() and target.stat().st_size > 8 * 1024 * 1024:
        target.rename(DATA_DIR / "api_traffic_audit.previous.jsonl")
    with target.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def api(session, method, path, payload=None, expected=(200, 201, 204), retries=2):
    url = API_BASE + path
    # Match the browser's study API calls.  In particular, omit Origin: it can
    # turn a normal same-site request into a rejected cross-origin-looking one.
    headers = {"Accept": "application/json", "Content-Type": "application/json",
               "Referer": API_BASE + "/course/" + COURSE_CODE}
    last_error = None
    for attempt in range(retries + 1):
        try:
            if method == "GET":
                r = session.get(url, headers=headers, timeout=TIMEOUT)
            elif method == "PUT":
                # Mutations are deliberately not retried to avoid duplicate writes.
                r = session.put(url, json=payload, headers=headers, timeout=TIMEOUT)
            else:
                raise ValueError("Unsupported HTTP method: " + method)
            if r.status_code in (401, 403):
                raise RuntimeError(f"{method} {path}: HTTP {r.status_code} — الجلسة غير صالحة.")
            try:
                body = r.json() if r.content else None
            except ValueError:
                body = r.text[:2000]
            if is_html_response(r, body):
                # Do not retain the app shell as fake API data or let a later
                # parser produce a misleading "chapters" error.
                snapshot = response_snapshot(r) or {}
                body = {"invalid_response": "html", "content_type": r.headers.get("Content-Type", ""),
                        "final_url": snapshot.get("final_url")}
                audit_api(method, path, payload, r, body)
                raise RuntimeError(
                    f"{method} {path}: خادم QUREO أعاد صفحة HTML بدل بيانات API؛ "
                    "سجّل الدخول من جديد وتحقق من اتصال منصة الدراسة."
                )
            audit_api(method, path, payload, r, body)
            if r.status_code not in expected:
                raise RuntimeError(f"{method} {path}: HTTP {r.status_code}: {safe_excerpt(r)}")
            return {"status": r.status_code, "body": body}
        except requests.RequestException as exc:
            last_error = exc
            if method != "GET" or attempt >= retries:
                break
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"تعذر تنفيذ {method} {path}: {last_error}")


def as_list(data, *keys):
    """Read a known list from the API's supported response envelopes.

    QUREO recordings show both a direct ``{chapters: [...]}`` form and wrapped
    forms such as ``{section: {chapters: [...]}}``.  Prefer a non-empty match,
    but preserve an explicitly empty list when that is all the server returned.
    This stays intentionally limited to structural envelope names and never
    guesses a list from an unrelated response field.
    """
    seen, empty = set(), []

    def find(value):
        nonlocal empty
        if isinstance(value, list):
            if value:
                return value
            empty = value
            return None
        if not isinstance(value, dict) or id(value) in seen:
            return None
        seen.add(id(value))
        for key in keys:
            if key in value:
                found = find(value[key])
                if found:
                    return found
        for key in ("data", "result", "items", "section", "course", "payload", "response"):
            if key in value:
                found = find(value[key])
                if found:
                    return found
        return None

    return find(data) or empty


def is_completed(row):
    if not isinstance(row, dict):
        return False
    if row.get("completed_at"):
        return True
    status = str(row.get("status", "")).lower()
    return status in {"completed", "complete", "done", "finished"}


def id_of(row, *keys):
    if not isinstance(row, dict):
        return None
    for key in keys:
        value = row.get(key)
        if value is not None:
            return str(value)
    return None


def fetch_chapter_states(session):
    progress = api(session, "GET", f"/api/study/students/sections/{SECTION_ID}/chapters")["body"]
    save_json("runtime_progress.json", progress)
    return as_list(progress, "chapters", "progress")


def chapter_list_from_response(payload):
    """Find only a structurally valid chapter collection in a QUREO response.

    The platform has returned both named lists and wrapped collections.  A valid
    chapter row must expose an ID and an integer-like sequence; this prevents a
    lessons/progress list from ever being mistaken for the chapter catalog.
    """
    seen = set()

    def is_chapter(row):
        if not isinstance(row, dict):
            return False
        if id_of(row, "id", "chapter_id") is None:
            return False
        if not any(key in row for key in ("chapter_id", "section_id", "test_type", "question_count")):
            return False
        try:
            seq = row.get("seq")
            if seq is None:
                return False
            int(seq)
            return True
        except (TypeError, ValueError):
            return False

    def walk(value):
        if isinstance(value, list):
            if value and all(is_chapter(row) for row in value):
                return value
            for item in value:
                found = walk(item)
                if found:
                    return found
            return None
        if not isinstance(value, dict) or id(value) in seen:
            return None
        seen.add(id(value))

        # Prefer known collection keys before inspecting a wrapped response.
        for key in ("chapters", "chapter_list", "chapterList", "items", "data", "result"):
            if key in value:
                candidate = value[key]
                if isinstance(candidate, dict):
                    rows = list(candidate.values())
                    if rows and all(is_chapter(row) for row in rows):
                        return rows
                found = walk(candidate)
                if found:
                    return found
        for child in value.values():
            found = walk(child)
            if found:
                return found
        return None

    return walk(payload) or []


def response_shape(payload, depth=0):
    """Non-sensitive structure for an actionable failure message."""
    if depth >= 2:
        return type(payload).__name__
    if isinstance(payload, list):
        return {"list_length": len(payload), "first": response_shape(payload[0], depth + 1) if payload else None}
    if isinstance(payload, dict):
        return {key: response_shape(value, depth + 1) for key, value in list(payload.items())[:20]}
    return type(payload).__name__


def fetch_chapters(session):
    section = api(session, "GET", f"/api/study/sections/{SECTION_ID}")["body"]
    states = fetch_chapter_states(session)
    save_json("runtime_section.json", section)
    chapters = chapter_list_from_response(section)
    if not chapters:
        save_json("runtime_section_shape.json", response_shape(section))
        raise RuntimeError("تعذر إيجاد قائمة فصول صالحة في رد القسم؛ لم يتم إرسال أي إجابة. "
                           "أرسل ملف data/runtime_section_shape.json فقط للمراجعة.")
    return sorted(chapters, key=lambda x: int(x.get("seq", 0))), states


def chapter_state_summary(state):
    if not isinstance(state, dict):
        return {"state": "missing"}
    # Keep only non-sensitive progression fields; never dump IDs/tokens or arbitrary response data.
    keys = ("status", "completed", "is_completed", "completed_at", "test_completed", "review_completed", "progress")
    return {key: state[key] for key in keys if key in state}


def run_chapter_lectures(session, chapter):
    chapter_id = str(chapter.get("id") or chapter.get("chapter_id") or "")
    if not chapter_id:
        raise RuntimeError("فصل بدون chapter_id؛ إيقاف آمن.")
    chapter_data = api(session, "GET", f"/api/study/chapters/{chapter_id}")["body"]
    lectures = as_list(chapter_data, "lectures")
    print(f"\n[CHAPTER] {chapter_id} | {chapter.get('name', 'بدون اسم')} | محاضرات: {len(lectures)}")
    for lecture in lectures:
        lecture_id = id_of(lecture, "id", "lecture_id")
        if not lecture_id:
            print("[SKIP] محاضرة بدون ID.")
            continue
        # Read the live progress immediately before every lecture. Files under
        # data/ are diagnostics only and are never consulted for decisions.
        live_state = api(session, "GET", f"/api/study/students/chapters/{chapter_id}/lectures")["body"]
        live_rows = as_list(live_state, "lectures", "lecture_progress", "progress")
        live_row = next((row for row in live_rows
                         if id_of(row, "lecture_id", "id") == lecture_id), None)
        if live_row is not None and is_completed(live_row):
            print(f"[SKIP-DONE] {lecture_id}")
            continue
        print(f"[START] {lecture_id}")
        try:
            api(session, "PUT", f"/api/study/students/lectures/{lecture_id}")
        except RuntimeError as exc:
            # QUREO uses HTTP 400 + already_started=true for the normal case
            # where a student closed the tool while this lecture was open. It
            # is not a failed prerequisite: continue that exact lecture and
            # let the existing completion/verification requests decide.
            error_text = str(exc).lower()
            if "prev_chapter_not_completed" in error_text or "prev_lecture_not_completed" in error_text:
                print(f"[PREREQUISITE-BLOCKED] {lecture_id}: الخادم أكد أن المتطلب السابق غير مكتمل؛ "
                      "تم تحديث الحالة الحية ولن يتم تخطي الفصل.")
                return False
            if "already_started" not in error_text:
                raise
            print(f"[RESUME-OPEN] {lecture_id} — استكمال المحاضرة المفتوحة على الخادم.")
        details = api(session, "GET", f"/api/study/lectures/{lecture_id}")["body"]
        talks = as_list(details, "talks")
        for talk in talks:
            # Only replay an explicit value supplied by the live API. Never invent one.
            if isinstance(talk, dict) and talk.get("id") is not None and "value" in talk:
                api(session, "PUT", f"/api/study/students/lecture-talks/{talk['id']}", {"value": talk["value"]})
                time.sleep(DEFAULT_PAUSE)
        api(session, "PUT", f"/api/study/students/lectures/{lecture_id}/complete")
        save_json(f"lecture_{lecture_id}_details.json", details)
        print(f"[COMPLETE-SENT] {lecture_id}")
        time.sleep(DEFAULT_PAUSE)
        # Confirm this exact lecture before moving to the next one. This also
        # prevents a stale local snapshot from making the next decision.
        post_state = api(session, "GET", f"/api/study/students/chapters/{chapter_id}/lectures")["body"]
        post_rows = as_list(post_state, "lectures", "lecture_progress", "progress")
        post_row = next((row for row in post_rows
                         if id_of(row, "lecture_id", "id") == lecture_id), None)
        if post_row is None or not is_completed(post_row):
            print(f"[VERIFY-WARN] الخادم لم يؤكد اكتمال المحاضرة {lecture_id} بعد طلب الإكمال.")
            return False

    verify_data = api(session, "GET", f"/api/study/students/chapters/{chapter_id}/lectures")["body"]
    verified = as_list(verify_data, "lectures", "lecture_progress", "progress")
    save_json(f"chapter_{chapter_id}_lecture_status.json", verify_data)
    remaining = []
    for lec in lectures:
        lid = id_of(lec, "id", "lecture_id")
        row = next((x for x in verified if id_of(x, "lecture_id", "id") == lid), None)
        if row is None or not is_completed(row):
            remaining.append(lid)
    if remaining:
        print(f"[VERIFY-WARN] المنصة لم تؤكد اكتمال: {', '.join(remaining)}")
        return False
    print(f"[VERIFIED] كل محاضرات الفصل {chapter_id} مكتملة حسب API.")
    return True


def incomplete_chapters(results):
    """(chapter_id, reason) for chapters whose run did not end completed."""
    final, order = {}, []
    for row in results:
        cid = str(row.get("chapter_id"))
        if cid not in final:
            order.append(cid)
        assessment = row.get("assessment")
        if assessment is not None:
            final[cid] = (None if assessment.get("assessment_completed") or
                          assessment.get("status") in ("already_finalized_server", "already_finalized_local", "preview_only")
                          else assessment.get("stop_reason") or assessment.get("status"))
        elif row.get("status") in ("already_completed", "chapter_verified_completed",
                                   "assessment_verified_progress_pending"):
            final[cid] = None
        elif row.get("status"):
            final[cid] = final.get(cid) or row.get("reason") or row["status"]
    return [(cid, final[cid]) for cid in order if final.get(cid)]


def assessment_answers_verified(result):
    """True only when the assessment endpoint verified every live question.

    The chapter-progress list can lag the answer/result endpoint.  This is not
    a replacement for a result check: an incomplete, unparsed, wrong, or
    unresolved assessment never qualifies.
    """
    if not isinstance(result, dict) or result.get("stop_reason"):
        return False
    # A final result can be discovered before this run starts (normal resume).
    # In that case the assessment runner intentionally sends no new answers,
    # but the server has already confirmed completion and the chapter gate may
    # continue waiting for its progress list to catch up.
    if result.get("status") in ("already_finalized_server", "already_finalized_local"):
        return True
    questions, correct, wrong = result.get("questions"), result.get("correct"), result.get("wrong")
    return (isinstance(questions, int) and questions > 0 and
            isinstance(correct, int) and not isinstance(correct, bool) and correct == questions and
            isinstance(wrong, int) and not isinstance(wrong, bool) and wrong == 0)


def main():
    parser = argparse.ArgumentParser(description="MOODY QUREO Requests — إكمال المحاضرات فقط")
    parser.add_argument("--from-level", type=int, default=1)
    parser.add_argument("--to-level", type=int, default=42)
    parser.add_argument("--preview-only", action="store_true", help="اعرض المطابقات بدون إرسال إجابات")
    parser.add_argument("--retake", action="store_true", help="اطلب إعادة الاختبارات المكتملة لرفع الدرجة؛ قد يرفض الخادم إعادة المحاولة")
    parser.add_argument("--resume-current", action="store_true", help="مع --retake فقط: تابع المحاولة المفتوحة حاليًا على الخادم بدون إرسال طلب «ابدأ من البداية»")
    args = parser.parse_args()
    if args.resume_current and not args.retake:
        parser.error("--resume-current يعمل مع --retake فقط")
    if not (1 <= args.from_level <= args.to_level <= 54):
        parser.error("المستويات يجب أن تكون بين 1 و42، والبداية لا تتجاوز النهاية.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if args.retake and args.preview_only:
        parser.error("لا يمكن الجمع بين --retake و --preview-only")
    if not args.preview_only:
        if args.retake and args.resume_current:
            print("\n[WARNING] وضع متابعة المحاولة الحالية: لن يُرسل طلب «ابدأ من البداية»؛ ستُكمل الأداة المحاولة المفتوحة على الخادم بالترتيب.")
            confirm = input("اكتب RESUME للتأكيد أو أي شيء للإلغاء: ").strip()
            if confirm != "RESUME":
                print("[CANCEL] لم يتم إرسال أي إجابة.")
                return 0
        elif args.retake:
            print("\n[WARNING] وضع إعادة الاختبار: سيُنفذ نفس طلب زر «ابدأ من البداية» لكل فصل محدد، ثم يقارن حالة الطالب قبل البدء وبعده.")
            print("[WARNING] 204 أو work_in_process=true لا يثبتان وحدهما محاولة جديدة؛ لن تُرسل إجابات إلا إذا ثبت تغيّر الحالة (NEW_CONFIRMED).")
            confirm = input("اكتب RETAKE للتأكيد أو أي شيء للإلغاء: ").strip()
            if confirm != "RETAKE":
                print("[CANCEL] لم يتم طلب إعادة أي اختبار.")
                return 0
        else:
            print("\n[WARNING] سيتم بدء تقييمات الفصول وإرسال الإجابات المطابقة لمعرف السؤال من قاعدة البيانات.")
            print("[NOTE] قاعدة الإجابات مصدر خارجي غير رسمي؛ المنصة وحدها تؤكد صحة النتيجة.")
            confirm = input("اكتب SUBMIT للمتابعة أو أي شيء للإلغاء: ").strip()
            if confirm != "SUBMIT":
                print("[CANCEL] لم يتم إرسال أي إجابة.")
                return 0
    return run(args)


def run(args, credentials=None):
    """Whole run after argument validation and confirmation. Used by the CLI and by server.py."""
    global COURSE_CODE, SECTION_ID
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STOP_EVENT.clear()
    RUN_STATE["chapter"], RUN_STATE["results"] = None, []
    track = getattr(args, "track", "javascript")
    saved_course = (COURSE_CODE, SECTION_ID)
    COURSE_CODE, SECTION_ID = TRACKS[track]
    session = requests.Session()
    try:
        user = login(session) if credentials is None else login(session, credentials)
        save_json("student_session_check.json", {"authenticated": True, "response_type": type(user).__name__})
        me = api(session, "GET", "/api/study/students/me")["body"]
        save_json("student_me_runtime.json", me)
        chapters, states = fetch_chapters(session)
        state_by_id = {id_of(x, "chapter_id", "id"): x for x in states if id_of(x, "chapter_id", "id")}
        selected = [c for c in chapters if args.from_level <= int(c.get("seq", 0)) <= args.to_level]
        if not selected:
            raise RuntimeError("لم يتم العثور على فصول في النطاق المحدد.")
        results = []
        RUN_STATE["results"] = results
        db = assessment_runner.load_db()
        # ملفات الإجابات المعتمدة لجافاسكريبت وبايثون هي المصدر الوحيد للإجابات (answers/<track>/chapter_NN.txt)
        import text_bank as text_bank_module
        text_bank = text_bank_module.TextBank.load(track)
        print(f"[DB] تم تحميل {len(db)} سؤالًا بمفاتيح الفصل/السؤال.")
        for chapter in selected:
            if STOP_EVENT.is_set():
                print("[STOP-REQUESTED] أوقفت التشغيل بطلبك قبل الفصل التالي.")
                break
            cid = id_of(chapter, "id", "chapter_id")
            RUN_STATE["chapter"] = cid
            # The first chapter list is only a catalog snapshot. Refresh the
            # selected chapter's progress immediately before making any skip
            # or resume decision; persisted runtime_progress.json is never
            # used as a source of truth.
            try:
                fresh_states = fetch_chapter_states(session)
                fresh_by_id = {id_of(x, "chapter_id", "id"): x for x in fresh_states
                               if id_of(x, "chapter_id", "id")}
                state = fresh_by_id.get(cid, {})
            except Exception as exc:
                print(f"[STATE-ERROR] {cid}: تعذر قراءة حالة الفصل الحية: {exc}")
                results.append({"chapter_id": cid, "status": "state_refresh_failed", "error": str(exc)})
                print(f"[STOP-BLOCKED] {cid}: لا يمكن اتخاذ قرار اعتمادًا على حالة قديمة؛ توقفت هنا.")
                break
            chapter_was_completed = is_completed(state)
            if chapter_was_completed and not args.retake:
                # A completed lesson/progress row is not enough evidence that
                # the chapter's review or test is finalized.  The assessment
                # runner checks its own live result endpoint and only then
                # skips a verified final assessment.  This keeps end-of-level
                # reviews and lesson-plus-test chapters in the same run.
                print(f"[CHAPTER-CHECK] {cid} — الدروس مكتملة؛ سأتأكد من المراجعة/الاختبار قبل التخطي.")
            if chapter_was_completed and args.retake:
                print(f"[RETAKE] {cid} — الفصل مكتمل؛ سيتم طلب محاولة جديدة صراحةً.")
            if not args.preview_only:
                try:
                    ok = run_chapter_lectures(session, chapter)
                except Exception as exc:
                    ok = False
                    print(f"[LECTURE-ERROR] {cid}: {exc}")
                if not ok:
                    results.append({"chapter_id": cid, "status": "needs_review"})
                    print(f"[STOP-BLOCKED] {cid}: لم يتم تأكيد كل المحاضرات؛ توقفت هنا ولن أتخطى الفصل.")
                    break
            else:
                print(f"[PREVIEW] {cid}: فحص الأسئلة فقط؛ لن يتم بدء الاختبار أو تعديل المحاضرات.")

            # Complete the chapter assessment/review using exact question IDs in the local DB.
            try:
                assessment_result = assessment_runner.solve_assessment(
                    session, chapter, db, submit=not args.preview_only, retake=args.retake,
                    resume_current=args.resume_current, text_bank=text_bank,
                )
                results.append({"chapter_id": cid, "assessment": assessment_result})
                if args.retake and not args.preview_only and not assessment_result.get("assessment_completed"):
                    results.append({"chapter_id": cid, "status": "retake_incomplete",
                                    "reason": assessment_result.get("stop_reason") or assessment_result.get("status")})
                    print(f"[STOP-BLOCKED] {cid}: المحاولة لم تكتمل (status={assessment_result.get('status')}, "
                          f"attempt_state={assessment_result.get('attempt_state')}). توقفت هنا ولن أتخطى الفصل.")
                    save_json("run_report.json", results)
                    break
            except Exception as exc:
                print(f"[ASSESSMENT-ERROR] {cid}: {exc}")
                results.append({"chapter_id": cid, "status": "assessment_error", "error": str(exc)})
                print(f"[STOP-BLOCKED] {cid}: خطأ في التقييم؛ توقفت هنا ولن أتخطى الفصل.")
                break

            if args.preview_only:
                continue

            # Refresh server-side chapter progression after the assessment.
            # The progress collection may lag the already-verified assessment
            # result for a short period, so read it a few times before treating
            # it as a genuine chapter gate.
            fresh_state = None
            try:
                for refresh in range(3):
                    fresh_states = fetch_chapter_states(session)
                    fresh_by_id = {id_of(x, "chapter_id", "id"): x for x in fresh_states
                                   if id_of(x, "chapter_id", "id")}
                    fresh_state = fresh_by_id.get(cid)
                    if is_completed(fresh_state) or refresh == 2:
                        break
                    print(f"[CHAPTER-GATE-WAIT] {cid}: انتظار تحديث حالة الفصل من الخادم ({refresh + 1}/2).")
                    time.sleep(DEFAULT_PAUSE)
            except Exception as exc:
                print(f"[STATE-ERROR] {cid}: تعذر تحديث حالة الفصول: {exc}")
                results.append({"chapter_id": cid, "status": "state_refresh_failed", "error": str(exc)})
                print(f"[STOP-BLOCKED] {cid}: لا يمكن تأكيد حالة الفصل؛ توقفت هنا ولن أتخطى الفصل.")
                break
            if not is_completed(fresh_state):
                summary = chapter_state_summary(fresh_state)
                save_json(f"chapter_{cid}_gate_status.json", summary)
                if assessment_answers_verified(assessment_result):
                    results.append({"chapter_id": cid, "status": "assessment_verified_progress_pending",
                                    "state": summary})
                    print(f"[CHAPTER-GATE-PENDING] {cid}: كل إجابات التقييم مؤكدة صحيحة، "
                          "لكن قائمة التقدّم لم تتحدث بعد؛ سأتابع للفصل التالي.")
                    print(f"[INFO] ملخص حالة الفصل محفوظ في data/chapter_{cid}_gate_status.json")
                    continue
                results.append({"chapter_id": cid, "status": "lectures_done_chapter_gate_pending", "state": summary})
                print(f"[CHAPTER-GATE] محاضرات {cid} مكتملة، لكن حالة الفصل نفسها غير مكتملة على الخادم.")
                print("[STOP-BLOCKED] حالة الفصل غير مكتملة على الخادم؛ توقفت هنا ولن أتخطى الفصل.")
                print(f"[INFO] ملخص حالة الفصل محفوظ في data/chapter_{cid}_gate_status.json")
                break

            results.append({"chapter_id": cid, "status": "chapter_verified_completed"})
        save_json("run_report.json", results)
        incomplete = incomplete_chapters(results)
        if incomplete:
            print(f"\n[SUMMARY] فصول لم تكتمل ({len(incomplete)}):")
            for cid_, reason_ in incomplete:
                print(f"  - {cid_}: {reason_}")
            print("[SUMMARY] تفاصيل كل سؤال متوقف في data/assessment_<الفصل>_unresolved_<السؤال>.json")
        print("\n[FINISHED] انتهى التشغيل. راجع data/run_report.json وملفات assessment_*.json.")
        print("[NOTICE] الإجابات غير المطابقة لمعرف سؤال دقيق تُترك دون تخمين. راجع نتائج الخادم لكل فصل.")
    except KeyboardInterrupt:
        print("\n[STOP] أوقف المستخدم التشغيل.")
        return 130
    except Exception as exc:
        print("[ERROR]", exc)
        return 1
    finally:
        session.close()
        COURSE_CODE, SECTION_ID = saved_course
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
