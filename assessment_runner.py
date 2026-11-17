# -*- coding: utf-8 -*-
"""QUREO assessment runner. Uses exact question IDs from the bundled local DB.
Answers are suggestions from an unofficial source; every chapter result is checked
against QUREO. Unknown/ambiguous questions are left unanswered, never guessed.
"""
import json, re, time, unicodedata
from pathlib import Path
import main as core

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "question_answer_database.json"
LEDGER_PATH = ROOT / "data" / "assessment_ledger.json"

def load_ledger():
    try:
        return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def save_ledger(ledger):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LEDGER_PATH)

def state_has_open_attempt(payload):
    """Detect an existing attempt so the start endpoint is never called twice."""
    found = []
    def walk(node):
        if isinstance(node, dict):
            if node.get("work_in_process") is True or node.get("in_progress") is True:
                found.append(True)
            if node.get("started_at") or node.get("startedAt"):
                found.append(True)
            status = str(node.get("status", "")).lower()
            if status in {"started", "in_progress", "in-progress", "working"}:
                found.append(True)
            for value in node.values(): walk(value)
        elif isinstance(node, list):
            for value in node: walk(value)
    walk(payload)
    return bool(found)

def result_is_final(payload):
    """Conservative detector: only explicit final/result flags count as finished."""
    found = []
    def walk(node):
        if isinstance(node, dict):
            for key in ("visible_result", "is_completed", "completed", "review_completed", "test_completed"):
                if node.get(key) is True:
                    found.append(True)
            status = str(node.get("status", "")).lower()
            if status in {"completed", "complete", "finished", "passed"}:
                found.append(True)
            if node.get("completed_at"):
                found.append(True)
            for value in node.values(): walk(value)
        elif isinstance(node, list):
            for value in node: walk(value)
    walk(payload)
    return bool(found)


def result_question_correctness(payload):
    """Return only per-question correctness from a result response.

    A result also nests every answer *choice*, whose `correct` flags describe
    the option key rather than the student's answer.  Counting those produced
    misleading numbers such as 8 correct / 11 wrong for a seven-question
    assessment.  Only the answer result records are evidence of the attempt.
    """
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return []
    values = []
    for key in ("choice_results", "description_results"):
        for row in result.get(key) or []:
            if isinstance(row, dict) and isinstance(row.get("correct"), bool):
                values.append((str(row.get("question_id") or ""), row["correct"]))
    return values


def result_has_incorrect_answers(payload):
    values = result_question_correctness(payload)
    return bool(values) and any(correct is False for _, correct in values)


def norm(s):
    s = str(s or "").lower()
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\[\[/?(?:bdi|bold|italic|underline|copy)[^\]]*\]\]", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\u0600-\u06ff]+", " ", s)).strip()


def write(name, value):
    core.save_json(name, value)


def load_db():
    obj = json.loads(DB_PATH.read_text(encoding="utf-8"))
    return {(str(x.get("chapter_id")), str(x.get("question_id"))): x
            for x in obj.get("questions", [])}


def parse_questions(payload):
    """HAR-confirmed shape: [{question:{id,content,type},choices,descriptions}]."""
    if isinstance(payload, dict):
        for k in ("questions", "data", "items", "result"):
            if isinstance(payload.get(k), list):
                payload = payload[k]
                break
    out = []
    if not isinstance(payload, list):
        return out
    for row in payload:
        if not isinstance(row, dict):
            continue
        q = row.get("question") if isinstance(row.get("question"), dict) else row
        qid = q.get("id") or q.get("question_id") or row.get("question_id")
        if qid is None:
            continue
        choices = row.get("choices") or []
        descriptions = row.get("descriptions") or []
        out.append({
            "id": str(qid), "type": str(q.get("type") or ("choice" if choices else "description")),
            "content": q.get("content") or q.get("text") or "",
            "hints": q.get("hints") or [], "choices": choices,
            "descriptions": descriptions, "raw": row,
        })
    return out


def extract_description_answer(dbrow):
    """Extract only explicit answer fragments from a noisy reference page.

    Supports the source wording used by Level 8 ("الإجابة نكتب في المكان
    الفارغ") as well as earlier wording. It never falls back to the whole page.
    """
    raw = str(dbrow.get("suggested_answer") or "").strip()
    if not raw:
        return None

    # Single missing slot: accept the first standalone code/expression line
    # after a clearly labeled answer instruction.
    single_markers = (
        r"(?:الإجابة|الاجابه)\s*نكتب\s+في\s+(?:المكان\s+الفارغ|الفراغ|النواقص)",
        r"(?:نكتب|اكتب|أكتب|ضع|أدخل)\s+في\s+النواقص",
        r"(?:الإجابة|الاجابه)\s*[:：]",
    )
    for marker in single_markers:
        m = re.search(marker + r"\s*[:：]?\s*\n+([\s\S]*?)(?=\n\s*(?:ليصبح|شكل الكود النهائي|ليكون الكود|الف مبروك|لو محتاج|حل المستوى)|$)", raw, re.I)
        if m:
            lines = [x.strip().strip('`') for x in m.group(1).splitlines() if x.strip()]
            if lines:
                candidate = lines[0].rstrip(";")
                if len(candidate) <= 2000 and not re.search(r"[\u0600-\u06ff]|https?://", candidate):
                    return candidate

    # Explicit ordered fill-in list. Return newline-separated fragments only
    # when the source clearly labels the list and every extracted line is code.
    m = re.search(r"نكتب\s+النواقص\s+في\s+الاسطر\s+هكذا\s+بالترتيب\s*[:：]?\s*\n+([\s\S]*?)(?=\n\s*(?:ليصبح|شكل الكود النهائي|الف مبروك|لو محتاج|حل المستوى)|$)", raw, re.I)
    if m:
        lines = [x.strip().strip('`') for x in m.group(1).splitlines() if x.strip()]
        if lines and len(lines) <= 10 and all(
            len(x) <= 500 and not re.search(r"[\u0600-\u06ff]|https?://", x)
            for x in lines
        ):
            return "\n".join(x.rstrip(";") for x in lines)

    # A lone explicit else-if line is a safe compact fragment.
    candidates = [x.strip().strip('`').rstrip(';') for x in raw.splitlines()
                  if re.search(r"^else\s+if\s*\(", x.strip(), re.I)]
    if len(candidates) == 1 and len(candidates[0]) <= 2000:
        return candidates[0]
    return None


def exact_id_final_editor_code(q, dbrow):
    """Read an explicitly labelled final editor buffer from an exact-ID row.

    Older imported rows often contain both Arabic instructions and a final code
    buffer after ``ليصبح الكود``.  The old fragment parser quite rightly
    rejected that mixed prose.  This helper reads only that labelled final
    buffer, verifies it against the live starter editor, and never accepts
    trailing article/navigation prose.
    """
    raw = str((dbrow or {}).get("suggested_answer") or "")
    starter = live_description_code(q)
    if not raw or starter is None:
        return None
    labels = list(re.finditer(r"(?:ليصبح|ليكون|شكل الكود النهائي|ليصبح شكل الكود)[^\n]*\n+", raw, re.I))
    if not labels:
        return None
    tail = raw[labels[-1].end():]
    tail = re.split(r"\n\s*(?:الف مبروك|لو محتاج|حل المستوى|حل المستوي|اقرا ايضا|الوسوم|مشاركة|مقالات ذات صلة)",
                    tail, maxsplit=1, flags=re.I)[0]
    lines = [line.rstrip() for line in tail.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None
    candidate = "\n".join(lines).strip()
    if (len(candidate) > 2000 or re.search(r"[\u0600-\u06ff]|https?://", candidate)
            or not _balanced_code(candidate)):
        return None
    starter = starter.strip()
    compact = lambda value: re.sub(r"\s+", "", value)

    def keeps_editor_scaffold(before, after):
        """Confirm that a full replacement retains every substantive starter line.

        A description editor frequently contains an empty indented line inside
        an ``if`` block.  A completed program necessarily inserts lines there,
        so requiring the final buffer to literally start with the complete
        starter buffer wrongly rejects valid answers.  We instead require every
        non-structural line that QUREO prefilled (declarations, conditions and
        existing output) to still occur in the final code.  This runs only
        after the exact live chapter/question-ID and prompt checks below.
        """
        required = []
        for line in before.splitlines():
            token = compact(line)
            if token and token not in {"{", "}", "};"}:
                required.append(token)
        actual = {compact(line) for line in after.splitlines() if compact(line)}
        return bool(required) and all(token in actual for token in required)

    if starter and not keeps_editor_scaffold(starter, candidate):
        return None
    # The imported prompt and live prompt must still describe the same task;
    # question ID alone is not a content check.
    bank_q, live_q = norm((dbrow or {}).get("question")), norm(q.get("content"))
    if not bank_q or not live_q or not (bank_q == live_q or bank_q in live_q or live_q in bank_q):
        return None
    return candidate


def response_completion(response_body):
    if not isinstance(response_body, dict):
        return None, None, False
    result = response_body.get("result") if isinstance(response_body.get("result"), dict) else {}
    complete = response_body.get("complete") if isinstance(response_body.get("complete"), dict) else {}
    chapter = complete.get("chapter") if isinstance(complete.get("chapter"), dict) else {}
    correct = result.get("correct") if isinstance(result.get("correct"), bool) else None
    return correct, result.get("model_answer"), bool(chapter.get("completed_at"))


def live_description_code(q):
    """Return the editable starter code supplied by QUREO for this live question."""
    descriptions = q.get("descriptions") or []
    codes = [str(x.get("code") or "") for x in descriptions if isinstance(x, dict)]
    return codes[0] if len(codes) == 1 else None


def exact_description_solution(q, dbrow):
    """Build full editor contents for the seven description questions in levels 1-7.

    The question ID is not used alone: prompt tokens and the live starter code are
    checked as a guard against a changed/recycled question ID. The returned value
    is the complete editor buffer, not merely the inserted fragment.
    """
    qid = str(q.get("id"))
    content = norm(q.get("content", ""))
    starter = live_description_code(q)
    expected = {
        "90001020": (('good morning', 'وحدة التحكم'), 'console.log', 'console.log("Good morning");'),
        "90002030": (('19', '40', 'نتيجة الحساب'), 'console.log();', 'console.log(19+40);'),
        "90003030": (('number', 'أعلن'), '', 'let number;'),
        "90004020": (('hello', 'console'), '', 'console.log("Hello");'),
        "90005010": (('num', '5', 'سطرين'), '', 'let num = 5;\\nconsole.log(num);'),
        "90005040": (('num', '100', 'correct'), '', 'let num = 100;\\nif (num == 100){console.log("Correct");}'),
        "90006020": (('num', '10', 'win', 'lose'), '', 'let num = 5;\\nif (num == 10){console.log("Win");}\\nelse {console.log("Lose");}'),
        "90007020": (('num', '2', 'two', 'السطر الثالث'), 'let num = 2;\\nif (num == 1){console.log("One");}', None),
    }
    spec = expected.get(qid)
    if not spec or not all(norm(token) in content for token in spec[0]):
        return None
    expected_starter, answer = spec[1], spec[2]
    if starter is None:
        return None
    if expected_starter:
        expected_starter = expected_starter.replace("\\n", "\n")
    if expected_starter and starter.strip() != expected_starter:
        return None
    if qid == "90007020":
        if starter.strip() != expected_starter:
            return None
        answer = starter.rstrip() + '\nelse if (num == 2){console.log("Two");}'
    elif starter.strip() not in ("", expected_starter):
        # QUREO changed the prefilled editor: don't overwrite unknown user code.
        return None
    if not answer or len(answer) > 2000:
        return None
    return answer.replace('\\n', '\n')


def pick_answer(q, dbrow):
    if not dbrow:
        return None, "question_id_not_in_database"
    answer = dbrow.get("suggested_answer")
    if not answer:
        return None, "database_has_no_answer"
    qtype = q["type"].lower()
    if qtype == "choice":
        # QUREO may shuffle/reorder options between attempts. A database choice
        # ID or A/B/C/D position is not a stable answer key. Resolve the intended
        # option by its full visible content against THIS live question, then
        # send the live choice ID returned by the server.
        choices = q["choices"]
        def choice_key(value):
            value = unicodedata.normalize("NFKC", str(value or ""))
            value = re.sub(r"\[\[/?(?:bdi|bold|italic|underline|copy)[^\]]*\]\]", "", value)
            # Tags can be the answer itself (<img ...>, <p>...</p>), not just
            # formatting.  Strip QUREO's [[bdi]] wrapper above but keep code.
            return re.sub(r"\s+", " ", value).strip().casefold()

        target = choice_key(dbrow.get("matched_choice") or answer)
        exact = [c for c in choices if target and choice_key(c.get("content")) == target]
        if len(exact) == 1 and exact[0].get("id") is not None:
            return {"kind": "choice", "choice_id": int(exact[0]["id"]), "answer": answer,
                    "source_status": dbrow.get("source_verification")}, "live_choice_text_match"
        return None, "choice_not_mapped_uniquely_by_live_text"
    # Description answers must come from exact question ID. For noisy scraped
    # sources, extract the missing-slot fragment instead of sending the whole page.
    if qtype == "description":
        # Prefer a live-question-specific solution when QUREO exposes the starter
        # editor buffer. This covers the known level 1-7 descriptions whose
        # third-party answer text is explanatory prose rather than raw code.
        answer_code = exact_description_solution(q, dbrow)
        answer_reason = "live_prompt_and_starter_code_template" if answer_code else None
        if not answer_code:
            answer_code = extract_description_answer(dbrow)
            answer_reason = "exact_question_id"
        if not answer_code:
            # Bank code for this exact question ID, accepted only when the live prompt,
            # hints and starter editor confirm it. Nothing is invented.
            answer_code = bank_verified_description(q, dbrow)
            answer_reason = "bank_exact_id_live_prompt_verified"
        if not answer_code:
            answer_code = exact_id_final_editor_code(q, dbrow)
            answer_reason = "exact_id_labeled_final_editor_code" if answer_code else None
        if not answer_code:
            return None, "description_answer_not_safely_extractable"
        if len(answer_code) > 2000:
            return None, "description_answer_exceeds_2000_chars"
        return {"kind": "description", "answer_code": answer_code,
                "source_status": dbrow.get("source_verification")}, answer_reason
    return None, "unsupported_question_type"


def should_skip_prior_answer(question_id, answered_ids, retake=False):
    """Only skip ledger entries when resuming the same attempt, never on retake."""
    return (not retake) and str(question_id) in set(map(str, answered_ids or []))


# ---------------------------------------------------------------------------
# v7.2 — Attempt State layer, live route-kind resolution, diagnostics
# ---------------------------------------------------------------------------
UNKNOWN = "UNKNOWN"
NEW_CONFIRMED = "NEW_CONFIRMED"
EXISTING_IN_PROGRESS = "EXISTING_IN_PROGRESS"
COMPLETED = "COMPLETED"
ATTEMPT_STATES = (UNKNOWN, NEW_CONFIRMED, EXISTING_IN_PROGRESS, COMPLETED)

ANSWER_MARKER_KEYS = ("answer", "answered", "is_answered", "student_answer", "user_answer",
                      "answered_at", "answer_code", "selected_choice_id", "selected_choice",
                      "choice_id")


def work_flag(payload):
    """True/False when the server exposes an explicit work_in_process/in_progress flag, else None."""
    values = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("work_in_process", "in_progress"):
                if isinstance(node.get(key), bool):
                    values.append(node[key])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    if any(values):
        return True
    if values:
        return False
    return None


def row_has_answer_marker(row):
    """True when a live question row itself carries a recorded answer for the student."""
    if not isinstance(row, dict):
        return False
    nodes = [row]
    if isinstance(row.get("question"), dict):
        nodes.append(row["question"])
    for node in nodes:
        for key in ANSWER_MARKER_KEYS:
            value = node.get(key)
            if value not in (None, "", False, [], {}):
                return True
    return False


def count_answered_markers(questions):
    return sum(1 for q in questions if row_has_answer_marker(q.get("raw")))


def last_answered_question_id(state):
    """Return only the explicit server checkpoint for an ordered attempt."""
    seen = set()

    def walk(value):
        if isinstance(value, dict):
            if id(value) in seen:
                return None
            seen.add(id(value))
            for key in ("last_answered_question_id", "last_answered_id"):
                if value.get(key) is not None:
                    return str(value[key])
            for child in value.values():
                found = walk(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found is not None:
                    return found
        return None

    return walk(state)


def answered_prefix_from_state(questions, state):
    """Map QUREO's last-answer checkpoint to the exact live question prefix.

    The platform accepts answers strictly in order.  This never infers an
    arbitrary answered question: it uses a checkpoint supplied by the server
    and only when that ID occurs in the current live question list.
    """
    checkpoint = last_answered_question_id(state)
    ids = [str(q.get("id")) for q in questions]
    if checkpoint is None or checkpoint not in ids:
        return set(), checkpoint
    return set(ids[:ids.index(checkpoint) + 1]), checkpoint


def server_error_flag(message, name):
    """Read a boolean API error field without treating `false` as a failure."""
    return bool(re.search(r'"' + re.escape(name) + r'"\s*:\s*true\b', str(message), re.I))


def classify_attempt(pre_state, post_state, pre_marked=None, post_marked=None, started_by_request=True):
    """Return (state, evidence). A 204 or work_in_process=true alone never gives NEW_CONFIRMED.

    NEW_CONFIRMED needs independent evidence that our start request changed the server
    state: explicit work_in_process False -> True, or previously recorded answer markers
    that disappeared. Anything else is UNKNOWN (or EXISTING_IN_PROGRESS when we did not
    send a start request ourselves).
    """
    if post_state is None:
        return UNKNOWN, ["post_state_unavailable"]
    post_flag = work_flag(post_state)
    pre_flag = work_flag(pre_state) if pre_state is not None else None
    if post_flag is not True:
        if result_is_final(post_state) and post_flag is False:
            return COMPLETED, ["post_state_final_not_in_progress"]
        return UNKNOWN, ["post_state_not_in_progress"]
    if pre_marked and post_marked == 0:
        return NEW_CONFIRMED, ["answer_markers_cleared_after_start"]
    if pre_state is None:
        return UNKNOWN, ["pre_state_unavailable"]
    if not started_by_request:
        return (EXISTING_IN_PROGRESS, ["server_work_in_process_true_without_start_request"]) \
            if post_flag is True else (UNKNOWN, ["no_open_attempt"])
    if pre_flag is False:
        return NEW_CONFIRMED, ["work_in_process_false_before_start", "work_in_process_true_after_start"]
    if pre_flag is True:
        return UNKNOWN, ["already_in_progress_before_start_cannot_prove_reset"]
    return UNKNOWN, ["pre_state_has_no_work_in_process_flag"]


def find_route_kind(sources):
    """Resolve review/test from LIVE metadata only.

    sources: list of (label, payload). Returns (kind or None, info dict). Conflicting or
    missing metadata returns None; nothing is inferred from chapter numbers.
    """
    per_source = {}

    def walk(node, label):
        if isinstance(node, dict):
            for key in ("test_type", "assessment_type"):
                value = node.get(key)
                if isinstance(value, str) and value.strip().lower() in {"review", "test"}:
                    per_source.setdefault(label, set()).add(value.strip().lower())
            for value in node.values():
                walk(value, label)
        elif isinstance(node, list):
            for value in node:
                walk(value, label)

    for label, payload in sources:
        if payload is not None:
            walk(payload, label)
    kinds = set()
    for found in per_source.values():
        kinds |= found
    info = {label: sorted(found) for label, found in per_source.items()}
    if len(kinds) == 1:
        return next(iter(kinds)), info
    return None, info


_JS_WORDS = {
    "let", "const", "var", "if", "else", "for", "while", "do", "function", "return", "true", "false",
    "null", "undefined", "console", "log", "break", "continue", "switch", "case", "default",
    "typeof", "new", "Math", "alert", "prompt", "length",
}


def _balanced_code(code):
    stack, quote, escaped = [], None, False
    pairs = {")": "(", "]": "[", "}": "{"}
    for ch in code:
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
    return quote is None and not stack


def code_literals(code):
    strings = [a or b for a, b in re.findall(r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', code)]
    stripped = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', " ", code)
    numbers = re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", stripped)
    idents = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", stripped) if w not in _JS_WORDS]
    return strings, numbers, idents


def bank_verified_description(q, dbrow):
    """Accept the bank's code only if the live question confirms it (no invention).

    Requires: the bank row is for this exact question id (caller), the bank question text
    matches the live text, the bank answer is balanced code only, every string/number/
    identifier in it appears in the live prompt, and it is compatible with the live editor
    starter code (empty, or the answer already contains the starter).
    """
    answer = str((dbrow or {}).get("suggested_answer") or "").strip()
    if not answer or len(answer) > 2000 or "\n\n" in answer:
        return None
    if re.search(r"[\u0600-\u06ff]|https?://", answer) or not _balanced_code(answer):
        return None
    if len(answer.splitlines()) > 10:
        return None
    bank_q, live_q = norm((dbrow or {}).get("question")), norm(q.get("content"))
    if not bank_q or not live_q or not (bank_q == live_q or bank_q in live_q or live_q in bank_q):
        return None
    prompt = "\n".join([str(q.get("content") or "")] + [str(h) for h in (q.get("hints") or [])])
    strings, numbers, idents = code_literals(answer)
    if not (strings or numbers or idents):
        return None
    if any(s not in prompt for s in strings):
        return None
    if any(not re.search(r"(?<!\d)" + re.escape(n) + r"(?!\d)", prompt) for n in numbers):
        return None
    if any(i not in prompt for i in idents):
        return None
    starter = live_description_code(q)
    if starter is None:
        return None
    starter = starter.strip()
    if starter and not answer.startswith(starter):
        return None
    return answer


def question_diagnostic(cid, q, reason, dbrow=None, attempt_state=None):
    bank = None
    if dbrow:
        keys = ("question_id", "type", "question", "starter_code", "suggested_answer", "matched_choice",
                "answer_source", "source_verification", "status")
        bank = {k: dbrow.get(k) for k in keys}
    return core.redact({
        "chapter_id": cid, "question_id": q.get("id"), "type": q.get("type"), "reason": reason,
        "content": q.get("content"), "hints": q.get("hints"), "choices": q.get("choices"),
        "live_starter_code": live_description_code(q), "descriptions": q.get("descriptions"),
        "bank": bank, "attempt_state": attempt_state,
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })


def print_unresolved_question(q, reason):
    print(f"[UNRESOLVED] Q{q.get('id')} — {reason}")
    print("----- نص السؤال كاملًا -----")
    print(str(q.get("content") or ""))
    for hint in q.get("hints") or []:
        print(f"  تلميح: {hint}")
    starter = live_description_code(q)
    if starter is not None:
        print(f"  starter code الحي: {starter!r}")
    for choice in q.get("choices") or []:
        if isinstance(choice, dict):
            print(f"  اختيار {choice.get('id')}: {choice.get('content')}")
    print("----------------------------")


def new_attempt_record(entry, state, evidence, mode):
    attempts = entry.setdefault("attempts", [])
    previous = current_attempt(entry)
    if previous and not previous.get("completed"):
        previous["superseded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    record = {"attempt_key": f"{int(time.time() * 1000)}-{len(attempts) + 1}",
              "opened_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": mode,
              "state": state, "evidence": list(evidence), "answered_ids": [],
              "submissions": [], "completed": False}
    attempts.append(record)
    entry["current_attempt"] = record["attempt_key"]
    return record


def current_attempt(entry):
    key = entry.get("current_attempt")
    for record in entry.get("attempts", []):
        if record.get("attempt_key") == key:
            return record
    return None



def solve_assessment(session, chapter, db, submit=True, retake=False, resume_current=False, text_bank=None):
    cid = str(chapter.get("id") or chapter.get("chapter_id"))
    level = int(chapter.get("seq") or 0)
    now = lambda: time.strftime("%Y-%m-%dT%H:%M:%S")

    def finish(status, **extra):
        out = {"chapter_id": cid, "level": level, "status": status, "submitted": 0,
               "retake_requested": bool(retake), "assessment_completed": False}
        out.update(extra)
        return out

    ledger = load_ledger()
    entry = ledger.setdefault(cid, {"started": False, "finalized": False, "answered_ids": []})
    auto_retake = False
    prior_final_failed = False

    # Check server-side result and persistent local ledger before opening anything.
    for suffix in (() if retake else ("test/result", "review/result")):
        try:
            prior = core.api(session, "GET", f"/api/study/students/chapters/{cid}/{suffix}")["body"]
            write(f"assessment_{cid}_prior_{suffix.replace('/', '_')}.json", prior)
            if result_is_final(prior):
                if result_has_incorrect_answers(prior):
                    # A final *failed* test does not complete its chapter.  The
                    # old runner treated every `completed_at` as success, then
                    # stopped at the chapter gate forever.  The result is
                    # independent server evidence that this attempt ended, so
                    # start one fresh attempt using corrected full-editor code.
                    retake = True
                    auto_retake = True
                    prior_final_failed = True
                    entry["finalized"] = False
                    entry["server_final_result"] = False
                    entry["answered_ids"] = []
                    entry["last_failed_result_at"] = now()
                    save_ledger(ledger)
                    print(f"[ASSESSMENT-RETAKE] {cid}: الخادم أكد نتيجة نهائية بها إجابة خاطئة؛ سأبدأ محاولة جديدة واحدة بالإجابات المصححة.")
                    break
                entry["finalized"] = True
                entry["server_final_result"] = True
                save_ledger(ledger)
                print(f"[ASSESSMENT-SKIP] {cid}: نتيجة نهائية موجودة على الخادم؛ لن يُفتح التقييم مرة أخرى.")
                return finish("already_finalized_server", attempt_state=COMPLETED)
        except Exception:
            pass
    if entry.get("finalized") and not retake:
        # A local ledger is a convenience for resuming after a tool restart,
        # not proof that QUREO completed the chapter.  If this run did not
        # obtain a final result from the server above, discard the stale local
        # marker and inspect/start the live attempt instead.  This prevents a
        # permanent CHAPTER-GATE loop after an older interrupted attempt.
        if entry.get("server_final_result") is True:
            print(f"[ASSESSMENT-SKIP] {cid}: نتيجة نهائية مؤكدة من الخادم؛ لن يُفتح التقييم مرة أخرى.")
            return finish("already_finalized_local", attempt_state=COMPLETED)
        entry["finalized"] = False
        entry["started"] = False
        entry["answered_ids"] = []
        entry["local_finalized_cleared_at"] = now()
        save_ledger(ledger)
        print(f"[ASSESSMENT-LEDGER-RESET] {cid}: تجاهلت علامة اكتمال محلية غير مؤكدة؛ سأتحقق من التقييم الحي.")

    # Read-only snapshot of the student state BEFORE any start request.
    pre_state = None
    if submit:
        try:
            pre_state = core.api(session, "GET", f"/api/study/students/chapters/{cid}/test")["body"]
            write(f"assessment_{cid}_student_state_before_start.json", pre_state)
        except Exception as exc:
            print(f"[STATE-WARN] {cid}: تعذر قراءة حالة الطالب قبل البدء: {exc}")
    if not retake and not entry.get("started") and pre_state is not None and state_has_open_attempt(pre_state):
        entry["started"] = True
        save_ledger(ledger)
        print(f"[ASSESSMENT-RESUME] {cid}: الخادم يؤكد وجود محاولة مفتوحة؛ لن نرسل طلب بدء جديد.")

    # Chapter test metadata is read first; no assumed schema beyond observed fields.
    test_payload = core.api(session, "GET", f"/api/study/chapters/{cid}/test")["body"]
    questions = parse_questions(test_payload)
    if not questions:
        write(f"assessment_{cid}_unparsed.json", test_payload)
        print(f"[ASSESSMENT-STOP] {cid}: لم أتعرف على شكل الأسئلة؛ لم يتم إرسال أي إجابة.")
        return {"chapter_id": cid, "status": "unparsed_questions", "questions": 0,
                "submitted": 0, "assessment_completed": False}

    # review vs test comes from live metadata only (chapter row, chapter metadata, test payload).
    sources = [("chapter", chapter)]
    kind_info = {}
    try:
        # Always consulted (read-only) so a chapter row that disagrees with the chapter
        # metadata is detected as a conflict instead of silently trusting one of them.
        meta = core.api(session, "GET", f"/api/study/chapters/{cid}")["body"]
        sources.append(("chapter_meta", meta))
    except Exception as exc:
        kind_info["chapter_meta_error"] = str(exc)
    sources.append(("test_payload", test_payload))
    kind, kind_info2 = find_route_kind(sources)
    kind_info.update(kind_info2)
    if kind is None and submit:
        write(f"assessment_{cid}_route_kind_unresolved.json", core.redact(kind_info))
        print(f"[ROUTE-STOP] {cid}: لم أجد test_type واضحًا (review/test) في بيانات الخادم؛ لن يُبدأ التقييم ولن تُرسل إجابات.")
        return finish("route_kind_unresolved", questions=len(questions), route_evidence=kind_info)
    test_type = kind or "unknown"
    endpoint_plural = "review-questions" if kind == "review" else "test-questions"
    print(f"[ASSESSMENT-KIND] {cid}: {test_type} | أسئلة: {len(questions)}")

    attempt, attempt_state = None, None
    active_state = pre_state
    if submit:
        if retake and resume_current:
            # Explicit user mode: continue whatever attempt is open; never press start.
            if pre_state is None or work_flag(pre_state) is not True:
                print(f"[RESUME-STOP] {cid}: لا توجد محاولة مفتوحة حسب الخادم؛ لن يتم بدء محاولة جديدة في وضع المتابعة.")
                return finish("no_current_attempt", attempt_state=UNKNOWN, questions=len(questions))
            attempt = current_attempt(entry)
            if not attempt or attempt.get("completed"):
                attempt = new_attempt_record(entry, EXISTING_IN_PROGRESS,
                                             ["server_work_in_process_true", "origin_not_proven_resume_requested"],
                                             "resume_current")
            attempt_state = attempt.get("state") if attempt.get("state") in (NEW_CONFIRMED, EXISTING_IN_PROGRESS) \
                else EXISTING_IN_PROGRESS
            entry["started"] = True
            save_ledger(ledger)
            print(f"[RESUME-CURRENT] {cid}: متابعة المحاولة الحالية بدون طلب بدء ({attempt_state}).")
        elif retake:
            if work_flag(pre_state) is True and not prior_final_failed:
                print(f"[RETAKE-BLOCKED] {cid}: توجد محاولة مفتوحة على الخادم؛ لن أرسل طلب «ابدأ من البداية» مرة ثانية.")
                print("[RETAKE-BLOCKED] لإكمال المحاولة المفتوحة شغّل: --retake --resume-current")
                return finish("retake_blocked_open_attempt", attempt_state=EXISTING_IN_PROGRESS,
                              questions=len(questions))
            if work_flag(pre_state) is True and prior_final_failed:
                print(f"[ASSESSMENT-RETAKE] {cid}: حالة المحاولة القديمة ما زالت ظاهرة كنشطة، لكن النتيجة النهائية تؤكد أنها انتهت؛ سأعيد البدء.")
            pre_marked = count_answered_markers(questions)
            history = entry.setdefault("retake_history", [])
            history.append({"previous_answered_ids": list(entry.get("answered_ids", [])),
                            "previous_finalized": bool(entry.get("finalized")),
                            "requested_at": now()})
            started = core.api(session, "PUT", f"/api/study/students/chapters/{cid}/test")
            entry["started"] = True
            entry["last_start_status"] = started.get("status")
            entry["last_start_mode"] = "browser_start_from_beginning"
            entry["start_requested_at"] = now()
            save_ledger(ledger)
            time.sleep(core.DEFAULT_PAUSE)
            try:
                post_state = core.api(session, "GET", f"/api/study/students/chapters/{cid}/test")["body"]
                write(f"assessment_{cid}_student_state_after_start.json", post_state)
            except Exception as exc:
                print(f"[ASSESSMENT-STOP] {cid}: تعذر التحقق من حالة التقييم بعد PUT: {exc}")
                return finish("post_start_state_unverified", attempt_state=UNKNOWN, questions=len(questions))
            post_marked = None
            try:
                post_questions = parse_questions(core.api(session, "GET", f"/api/study/chapters/{cid}/test")["body"])
                post_marked = count_answered_markers(post_questions)
            except Exception:
                pass
            attempt_state, evidence = classify_attempt(pre_state, post_state, pre_marked, post_marked,
                                                       started_by_request=True)
            if prior_final_failed and attempt_state == UNKNOWN and work_flag(post_state) is True:
                # The API can keep the old work_in_process flag visible while
                # reset completes.  A previously fetched final failed result,
                # our successful reset request, and a new active state are
                # together sufficient evidence for this one automatic retake.
                attempt_state = NEW_CONFIRMED
                evidence = list(evidence) + ["prior_final_failed_result", "fresh_start_request_active"]
            entry["post_start_state"] = post_state
            entry["post_start_active"] = work_flag(post_state) is True
            entry["attempt_state"] = attempt_state
            entry["attempt_evidence"] = evidence
            if attempt_state != NEW_CONFIRMED:
                entry["retake_reset_verified"] = False
                save_ledger(ledger)
                print(f"[RETAKE_UNCONFIRMED] {cid}: حالة المحاولة {attempt_state} ({', '.join(evidence)}).")
                print("[RETAKE_UNCONFIRMED] لن تُرسل أي إجابات؛ لا يوجد دليل مستقل أن الخادم بدأ محاولة جديدة.")
                return finish("retake_unconfirmed", attempt_state=attempt_state, evidence=evidence,
                              questions=len(questions))
            entry["retake_reset_verified"] = True
            entry["finalized"] = False
            attempt = new_attempt_record(entry, NEW_CONFIRMED, evidence, "retake")
            active_state = post_state
            save_ledger(ledger)
            print(f"[RETAKE-NEW-CONFIRMED] {cid}: ({', '.join(evidence)}).")
        elif not entry.get("started"):
            started = core.api(session, "PUT", f"/api/study/students/chapters/{cid}/test")
            entry["started"] = True
            entry["last_start_status"] = started.get("status")
            entry["last_start_mode"] = "first_attempt"
            entry["start_requested_at"] = now()
            save_ledger(ledger)
            time.sleep(core.DEFAULT_PAUSE)
            try:
                post_state = core.api(session, "GET", f"/api/study/students/chapters/{cid}/test")["body"]
                write(f"assessment_{cid}_student_state_after_start.json", post_state)
            except Exception as exc:
                print(f"[ASSESSMENT-STOP] {cid}: تعذر التحقق من حالة التقييم بعد PUT: {exc}")
                return finish("post_start_state_unverified", attempt_state=UNKNOWN, questions=len(questions))
            entry["post_start_state"] = post_state
            entry["post_start_active"] = state_has_open_attempt(post_state)
            if not entry["post_start_active"]:
                save_ledger(ledger)
                print(f"[ASSESSMENT-STOP] {cid}: طلب البدء رجع {started.get('status')} لكن الخادم لم يؤكد work_in_process؛ لن تُرسل إجابات.")
                return finish("start_not_verified", attempt_state=UNKNOWN, questions=len(questions))
            attempt_state, evidence = classify_attempt(pre_state, post_state, started_by_request=True)
            attempt = new_attempt_record(entry, attempt_state, evidence, "first_attempt")
            active_state = post_state
            save_ledger(ledger)
            print(f"[ASSESSMENT-STARTED] {cid}: الخادم يؤكد وجود تقييم نشط ({attempt_state}).")
        else:
            attempt = current_attempt(entry)
            if not attempt or attempt.get("completed"):
                attempt = new_attempt_record(entry, EXISTING_IN_PROGRESS, ["resumed_from_ledger_or_server"], "resume")
            attempt_state = attempt.get("state")
            save_ledger(ledger)
            print(f"[ASSESSMENT-RESUME] {cid}: استكمال المحاولة الحالية بدون إعادة فتح التقييم.")

    # An interrupted browser/tool run can leave the first questions accepted on
    # QUREO while the local ledger is empty.  Use the server's explicit
    # checkpoint to continue immediately after that exact question.
    server_answered, checkpoint = answered_prefix_from_state(questions, active_state)
    if server_answered:
        known = {str(value) for value in entry.setdefault("answered_ids", [])}
        for qid in [str(q["id"]) for q in questions if str(q["id"]) in server_answered]:
            if qid not in known:
                entry["answered_ids"].append(qid)
                known.add(qid)
        if attempt is not None:
            attempt_known = {str(value) for value in attempt.setdefault("answered_ids", [])}
            for qid in [str(q["id"]) for q in questions if str(q["id"]) in server_answered]:
                if qid not in attempt_known:
                    attempt["answered_ids"].append(qid)
                    attempt_known.add(qid)
        save_ledger(ledger)
        print(f"[ASSESSMENT-CHECKPOINT] {cid}: الخادم أكد الإجابة حتى Q{checkpoint}; سأتابع من السؤال التالي.")

    details, unresolved, sent, seen = [], [], [], set()
    stop_reason = None
    attempt_completed = False
    for q in questions:
        if q["id"] in seen:
            continue
        seen.add(q["id"])
        if submit and core.STOP_EVENT.is_set():
            print("[STOP-REQUESTED] أوقفت الإرسال بطلبك؛ المحاولة الحالية تبقى مفتوحة كما هي.")
            stop_reason = "stopped_by_user"
            break
        if text_bank is not None:
            # Answer files are preferred because they track the student-facing
            # course.  A few live review prompts differ only in boilerplate or
            # translation from those files, though.  In that narrow case, fall
            # back only to the bundled row with the *same live chapter and
            # question ID*; pick_answer still verifies the live choices/code.
            dbrow = None
            answer, reason = text_bank.lookup(level, q)
            source, verification = "answers_files", "approved_answers_file"
            if not answer:
                dbrow = db.get((cid, q["id"]))
                fallback, fallback_reason = pick_answer(q, dbrow)
                if fallback:
                    answer, reason = fallback, "exact_question_id_fallback:" + fallback_reason
                    source, verification = "question_answer_database", "exact_live_question_id"
            record = {"question_id": q["id"], "question": q["content"], "type": q["type"],
                      "match": reason, "answer_source": source, "source_verification": verification}
        else:
            dbrow = db.get((cid, q["id"]))
            answer, reason = pick_answer(q, dbrow)
            record = {"question_id": q["id"], "question": q["content"], "type": q["type"],
                      "match": reason, "answer_source": (dbrow or {}).get("answer_source"),
                      "source_verification": (dbrow or {}).get("source_verification")}
        if not answer:
            unresolved.append(record)
            details.append(record)
            diag = question_diagnostic(cid, q, reason, dbrow, attempt_state)
            write(f"assessment_{cid}_unresolved_{q['id']}.json", diag)
            print_unresolved_question(q, reason)
            print(f"[DIAGNOSTIC] data/assessment_{cid}_unresolved_{q['id']}.json")
            print("[SEQUENCE-STOP] لن يتم إرسال أي سؤال لاحق قبل حل هذا السؤال؛ QUREO يشترط الإجابة بالترتيب.")
            stop_reason = "unresolved_question"
            break
        if not submit:
            record["proposed_answer"] = answer
            details.append(record)
            continue
        # Retake: only answers accepted in THIS attempt may be skipped. Normal resume keeps
        # the original behavior (skip IDs already accepted in the same single attempt).
        if retake:
            skip = str(q["id"]) in set(map(str, (attempt or {}).get("answered_ids", [])))
        else:
            skip = should_skip_prior_answer(q["id"], entry.get("answered_ids", []), retake=False)
        if skip:
            record["skipped"] = "already_accepted_in_current_attempt"
            details.append(record)
            print(f"[ANSWER-SKIP] Q{q['id']} — قُبلت في المحاولة الحالية؛ لن تتكرر.")
            continue
        if answer["kind"] == "choice":
            path = f"/api/study/students/{endpoint_plural}/{q['id']}/answer/choice"
            payload = {"choice_id": answer["choice_id"]}
        else:
            path = f"/api/study/students/{endpoint_plural}/{q['id']}/answer/description"
            code = answer["answer_code"]
            if len(code) > 2000:
                unresolved.append({**record, "reason": "answer_code_over_2000_chars", "length": len(code)})
                print(f"[UNRESOLVED] Q{q['id']} — answer_code أطول من 2000 حرف")
                details.append(record)
                stop_reason = "unresolved_question"
                break
            payload = {"answer_code": code}
            record["answer_code_length"] = len(code)
        try:
            result = core.api(session, "PUT", path, payload)
            record["submitted"] = True
            record["request_accepted"] = True
            record["http_status"] = result["status"]
            correct, model_answer, chapter_completed = response_completion(result.get("body"))
            record["answer_correct"] = correct
            if correct is not None:
                record["server_correct"] = correct
            if model_answer:
                record["server_model_answer"] = model_answer
            sent.append(record)
            entry.setdefault("answered_ids", []).append(q["id"])
            if attempt is not None:
                attempt["answered_ids"].append(q["id"])
                attempt["submissions"].append({"question_id": q["id"], "http_status": result["status"],
                                               "request_accepted": True, "answer_correct": correct,
                                               "at": now()})
            if chapter_completed:
                attempt_completed = True
                entry["finalized"] = True
                entry["server_final_result"] = True
                entry["completed_at_from_answer"] = True
                if attempt is not None:
                    attempt["completed"] = True
                    attempt["state"] = COMPLETED
            save_ledger(ledger)
            if correct is False:
                print(f"[ANSWER-WRONG] Q{q['id']}: الخادم أكد أن الإجابة خاطئة؛ سيتوقف الفصل فورًا.")
                details.append(record)
                write(f"assessment_{cid}_answers.json", details)
                stop_reason = "answer_wrong"
                break
            print(f"[ANSWER-SENT] Q{q['id']} ({reason}) — REQUEST_ACCEPTED"
                  + (" | ANSWER_CORRECT" if correct is True else " | correctness_unconfirmed"))
            if chapter_completed:
                print(f"[CHAPTER-COMPLETE] {cid}: الخادم أكد اكتمال الفصل في استجابة الإجابة (ASSESSMENT_COMPLETED).")
        except Exception as exc:
            message = str(exc)
            # A stale local ledger can race with an answer accepted by the
            # server.  Confirm the exact ordered checkpoint before continuing;
            # never skip merely because an error message contains a word.
            if server_error_flag(message, "already_answered"):
                try:
                    live_state = core.api(session, "GET", f"/api/study/students/chapters/{cid}/test")["body"]
                    known_after_error, live_checkpoint = answered_prefix_from_state(questions, live_state)
                except Exception:
                    known_after_error, live_checkpoint = set(), None
                if str(q["id"]) in known_after_error:
                    record["submitted"] = False
                    record["request_accepted"] = True
                    record["skipped"] = "already_answered_on_server"
                    entry_known = {str(value) for value in entry.setdefault("answered_ids", [])}
                    for qid in [str(item["id"]) for item in questions if str(item["id"]) in known_after_error]:
                        if qid not in entry_known:
                            entry["answered_ids"].append(qid)
                            entry_known.add(qid)
                    if attempt is not None:
                        attempt_known = {str(value) for value in attempt.setdefault("answered_ids", [])}
                        for qid in [str(item["id"]) for item in questions if str(item["id"]) in known_after_error]:
                            if qid not in attempt_known:
                                attempt["answered_ids"].append(qid)
                                attempt_known.add(qid)
                    save_ledger(ledger)
                    details.append(record)
                    print(f"[ANSWER-ALREADY-SAVED] Q{q['id']} — الخادم أكد الحفظ حتى Q{live_checkpoint}; متابعة السؤال التالي.")
                    continue
            record["submitted"] = False
            record["request_accepted"] = False
            record["error"] = message
            unresolved.append(record)
            details.append(record)
            if server_error_flag(message, "prev_question_not_answered"):
                record["reason"] = "prev_question_not_answered"
                stop_reason = "prev_question_not_answered"
                print(f"[PREV-QUESTION-NOT-ANSWERED] Q{q['id']}: الخادم يقول إن السؤال السابق لم يُسجَّل في المحاولة الحالية.")
            else:
                stop_reason = "answer_error"
                print(f"[ANSWER-ERROR] Q{q['id']}: {message}")
            print("[SEQUENCE-STOP] تم إيقاف الفصل عند أول خطأ إرسال لتجنب أخطاء prev_question_not_answered.")
            write(f"assessment_{cid}_answers.json", details)
            break
        details.append(record)
        time.sleep(core.DEFAULT_PAUSE)

    write(f"assessment_{cid}_answers.json", details)
    correct_count = wrong_count = None
    if submit:
        if attempt_completed:
            status = "server_completed_from_answer"
        else:
            print(f"[ASSESSMENT-INCOMPLETE] {cid}: لا يوجد تأكيد اكتمال من الخادم؛ لن نرسل طلب إنهاء إضافيًا أو نفتح محاولة جديدة.")
            result_payload = None
            result_errors = []
            for suffix in ("test/result", "review/result"):
                try:
                    result_payload = core.api(session, "GET", f"/api/study/students/chapters/{cid}/{suffix}")["body"]
                    break
                except Exception as exc:
                    result_errors.append(str(exc))
            if result_payload is not None:
                write(f"assessment_{cid}_server_result.json", result_payload)
                correctness = result_question_correctness(result_payload)
                correct_count = sum(correct for _, correct in correctness)
                wrong_count = len(correctness) - correct_count
                status = "server_result_verified" if correctness else "result_saved_unparsed"
                # Outside a retake a final server result proves completion; during a retake an
                # old final result can still be visible, so it is never used as proof.
                if not retake and result_is_final(result_payload) and not stop_reason:
                    if wrong_count:
                        status = "server_result_wrong_answers"
                        entry["finalized"] = False
                        entry["server_final_result"] = False
                        print(f"[ASSESSMENT-FAILED] {cid}: الخادم أكد {wrong_count} إجابة خاطئة؛ الفصل لن يُعتبر مكتملًا.")
                    else:
                        attempt_completed = True
                        if attempt is not None:
                            attempt["completed"] = True
                            attempt["state"] = COMPLETED
            else:
                status = "submitted_result_unavailable"
                write(f"assessment_{cid}_result_errors.json", result_errors)
        save_ledger(ledger)
    else:
        status = "preview_only"
    confirmed_correct = sum(1 for r in sent if r.get("answer_correct") is True)
    return {"chapter_id": cid, "level": level, "test_type": test_type, "status": status,
            "retake_requested": bool(retake), "attempt_state": COMPLETED if attempt_completed else attempt_state,
            "stop_reason": stop_reason,
            "questions": len(questions), "submitted": len(sent), "unresolved": len(unresolved),
            "request_accepted": len(sent), "answers_correct_confirmed": confirmed_correct,
            "assessment_completed": bool(attempt_completed),
            "correct": correct_count, "wrong": wrong_count,
            "finalized": bool(entry.get("finalized")), "answered_ids": entry.get("answered_ids", [])}
