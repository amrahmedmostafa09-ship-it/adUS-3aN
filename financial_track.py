# -*- coding: utf-8 -*-
"""مسار الثقافة المالية داخل Qureo Solver Tool.

يشغّل financial/financial_literacy_.py كما هو (Playwright + answers.json) في عملية منفصلة، ويحوّل مخرجاته إلى
أحداث الواجهة (إجابات مرسلة، المستوى الحالي، قائمة المستويات). بيانات الدخول تُمرَّر بمتغيرات البيئة فقط
(لا تظهر في سطر الأوامر) وتُخفى من السجل المعروض.
"""
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import main as core
import financial_api

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "financial" / "financial_literacy_.py"
ANSWERS = HERE / "financial" / "answers.json"
# المنصة تعرض ستة فصول فقط لمسار الثقافة المالية، حتى لو كانت قاعدة
# الإجابات المحلية تحتوي على مستويات إضافية للاستخدامات القديمة/المرجعية.
MAX_FIN_LEVEL = 6
_QUESTION = re.compile(r"^\[L(\d+)\s+(\w+)\s+Q(\d+)\]\s*(.*)$")


def find_chrome():
    """مسار متصفح Chromium محلي (Chrome أو Edge)، أو None."""
    override = os.environ.get("CHROME_PATH", "").strip()
    if override and Path(override).is_file():
        return override
    for name in ("google-chrome", "google-chrome-stable", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    if os.name == "nt":
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:
        candidates.append(Path("/opt/google/chrome/chrome"))
    return next((str(c) for c in candidates if c.is_file()), None)


def check_available(script=None, answers=None):
    """None لو ملفات المسار جاهزة، وإلا رسالة عربية بسبب عدم الجاهزية."""
    # The default runner is direct HTTPS API (from the supplied HAR), not the
    # old browser-clicking fallback.  Do not make a usable financial run depend
    # on Chromium or Playwright.
    if importlib.util.find_spec("requests") is None:
        return "الثقافة المالية محتاجة مكتبة requests: pip install requests"
    try:
        financial_api.AnswerBank.load(financial_api.ANSWERS)
    except financial_api.FinancialApiError:
        return "ملفات الثقافة المالية ناقصة أو غير صالحة (مجلد financial/lessons)."
    return None


def _kill_tree(proc):
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            # The Stop button must return promptly.  taskkill removes Chrome's
            # children too, but it must not hold the UI thread for ten seconds
            # if Windows is already tearing the process down.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=3)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def run(hi, creds, stop_event, script=None, answers=None, extra_env=None, preview_only=False):
    """يحل حتى المستوى hi (يكمل من حيث وصل الطالب على الموقع). يرجع كود خروج العملية."""
    # لا نطلب من المنصة أي مستوى بعد الفصل السادس، حتى لو وصل حد أكبر من
    # واجهة قديمة أو استدعاء خارجي بالخطأ.
    hi = min(int(hi), MAX_FIN_LEVEL)
    # كل تشغيل جديد يبدأ بحالة إيقاف نظيفة. بدون ذلك، قد تنتقل ضغطة إيقاف
    # من تشغيل مالي سابق إلى التشغيل التالي وتوقفه فورًا رغم عدم طلب الإيقاف.
    stop_event.clear()
    if script is None and answers is None and extra_env is None:
        # The production path mirrors JavaScript/Python: API login, current
        # server state, one confirmed mutation per answer, then completion.
        # The Playwright process below is retained only for isolated legacy
        # tests/custom scripts, never selected by the web application.
        return financial_api.run(hi, creds, stop_event, preview=preview_only)
    script, answers = Path(script or SCRIPT), Path(answers or ANSWERS)
    work = core.DATA_DIR / "financial_run"            # لقطات الشاشة التي يكتبها السكربت تروح هنا
    work.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"SITE_USER": creds[0], "SITE_PASS": creds[1], "HEADLESS": "true", "MAX_LEVEL": str(hi),
                "ANSWERS_JSON": str(answers), "BROWSER_CHANNEL": "chrome", "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1"})
    env.pop("START_LEVEL", None)
    env.update(extra_env or {})
    browser_path = find_chrome()
    if browser_path:
        # Prefer the actual discovered executable over Playwright's named
        # channel.  This handles a normal Chrome/Edge installation that is
        # absent from PATH or registered under a nonstandard location.
        env["CHROME_PATH"] = browser_path
    else:
        # Use Playwright's managed Chromium when Chrome/Edge is not installed.
        # run_ui_windows.bat prepares it on first use.
        env["BROWSER_CHANNEL"] = ""
    try:
        no_output_timeout = max(5, int(env.get("FINANCE_NO_OUTPUT_TIMEOUT", "45")))
    except ValueError:
        no_output_timeout = 45

    core.RUN_STATE["chapter"], core.RUN_STATE["results"] = None, []
    results = core.RUN_STATE["results"]

    kwargs = {"start_new_session": True} if os.name == "posix" else \
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    print("[FINANCE] جاري تجهيز متصفح الثقافة المالية…")
    proc = subprocess.Popen([sys.executable, "-u", str(script)], cwd=str(work), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                            errors="replace", bufsize=1, **kwargs)
    done_flag = threading.Event()
    stalled = threading.Event()
    last_output = [time.monotonic()]

    def watcher():
        while not done_flag.is_set():
            if stop_event.wait(0.2):
                if proc.poll() is None:
                    _kill_tree(proc)
                return
            if proc.poll() is None and time.monotonic() - last_output[0] >= no_output_timeout:
                stalled.set()
                _kill_tree(proc)
                return

    threading.Thread(target=watcher, daemon=True).start()

    current, stop_line, finished_ok = None, None, False
    try:
        for raw in proc.stdout:
            last_output[0] = time.monotonic()
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            m = _QUESTION.match(line.strip())
            if m:
                level = int(m.group(1))
                if level != current:
                    if current is not None:
                        results.append({"chapter_id": f"L{current}",
                                        "assessment": {"assessment_completed": True, "status": "level_finished"}})
                    current = level
                    core.RUN_STATE["chapter"] = f"L{level}"
                print(f"[ANSWER-SENT] L{level} {m.group(2)} Q{m.group(3)} — {m.group(4)}")
            elif "⛔" in line:
                stop_line = line.replace("⛔", "").strip()
                print(f"[UNRESOLVED] {stop_line}")
            elif "تم تسجيل الدخول" in line:
                print(f"[LOGIN] {line.strip()}")
            elif line.startswith("خطأ") or "انتهى الوقت" in line:
                print(f"[ERROR] {line.strip()}")
            elif "وصلت للمستوى" in line:
                finished_ok = True
                print(f"[CHAPTER-COMPLETE] {line.strip()}")
            elif "ملخص الأسئلة" in line:
                if not stop_line:
                    finished_ok = True
                print(f"[SUMMARY] {line.strip()}")
            else:
                print(line)
    finally:
        code = proc.wait()
        done_flag.set()
        proc.stdout.close()
    if stalled.is_set():
        print(f"[ERROR] الثقافة المالية لم ترسل أي تقدم لمدة {no_output_timeout} ثانية؛ أوقفت التشغيل بدل الانتظار بلا نهاية.")
    if current is not None:
        stopped_by_user = stop_event.is_set()
        ok = finished_ok and code == 0 and not stop_line and not stopped_by_user
        entry = {"assessment_completed": ok,
                 "status": "level_finished" if ok else "level_incomplete"}
        if not ok:
            entry["stop_reason"] = ("stopped_by_user" if stopped_by_user else
                                    "stopped_at_question" if stop_line else "script_error")
        results.append({"chapter_id": f"L{current}", "assessment": entry})
    return code
