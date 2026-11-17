# -*- coding: utf-8 -*-
"""Direct, state-checked API runner for the Financial Literacy track.

The request shapes in this module are taken from the supplied HAR recording.
Unlike the retired browser-clicking path, it uses the same request/response
pattern as the Qureo tracks: authenticate, read current server state, submit
one verified answer at a time, and stop at an unknown question.
"""
from __future__ import annotations

import json
import html
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import requests

import main as core


BASE = "https://egy.app.spx-learning-square.com"
SUBJECT_ID = 7
MAX_LEVEL = 22
TIMEOUT = 30
HERE = Path(__file__).resolve().parent
ANSWERS = HERE / "financial" / "lessons"
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_PLATFORM_MARKUP = re.compile(r"\[\[/?(?:bdi|bold|italic|underline|copy|code)[^\]]*\]\]|<[^>]+>")
# Live financial options often start with a visible blank number such as
# ``①``/``1`` while the answer bank stores only the text for blank (1). Keep
# later markers (for example ``②``) because they are part of the answer.
_CHOICE_PREFIX = re.compile(
    r"^(?:[\(\[]?[أاA-Za-z0-9٠-٩]+[\)\].:：、-]\s*|[•●▪◦]|[-–—])",
    re.I,
)


class FinancialApiError(RuntimeError):
    """A non-recoverable, user-readable API state or answer mismatch."""

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.path = path


def norm(value: object) -> str:
    value = html.unescape(_PLATFORM_MARKUP.sub(" ", str(value or "")))
    value = unicodedata.normalize("NFKC", value).translate(_AR_DIGITS)
    value = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", value)
    value = re.sub("[أإآٱ]", "ا", value).replace("ى", "ي").replace("ة", "ه")
    value = re.sub(r"[^\w\s]|_", " ", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def option_norm(value: object) -> str:
    """Normalize an option while ignoring only a leading UI choice marker."""
    value = html.unescape(_PLATFORM_MARKUP.sub(" ", str(value or "")))
    value = unicodedata.normalize("NFKC", value).translate(_AR_DIGITS)
    value = re.sub(r"\s+", " ", value).strip().casefold()
    value = _CHOICE_PREFIX.sub("", value).strip()
    # Keep leading numbers here. They may be real values (3 years, 240
    # pounds) or selected combination members (② و③). Blank-marker removal
    # is handled only when comparing a digit-prefixed option to a
    # non-digit-prefixed model answer.
    # Arabic UI text may render a conjunction as ``② و ③`` or ``②و③``.
    # Treat those display-only spaces identically for combination answers.
    return re.sub(r"\s*و\s*", "و", value)


def option_punctuation_key(value: object) -> str:
    """Compare options while treating punctuation-only differences as equal."""
    return re.sub(r"[^\w\s]", "", option_norm(value)).replace(" ", "")


def coverage(needle: list[str], haystack: list[str]) -> float:
    total = sum(len(word) for word in needle)
    if not total:
        return 0.0
    matched = 0
    for block in SequenceMatcher(None, needle, haystack, autojunk=False).get_matching_blocks():
        if not block.size:
            continue
        chars = sum(len(word) for word in needle[block.a:block.a + block.size])
        if block.size >= 2 or chars >= 5:
            matched += chars
    return matched / total


def option_score(answer: str, option: str) -> float:
    if not option:
        return 0.0
    option = option_norm(norm(option))
    answer = option_norm(norm(answer))
    if answer == option:
        return 1.0
    # A bank answer may omit the visual marker for the first blank while the
    # live option contains it (``text`` vs ``1 text``). Strip that marker only
    # when exactly one side starts with a single digit; never strip digits
    # from genuine values such as ``3 years`` vs ``4 years`` or ``240``.
    def without_blank_marker(value: str) -> str:
        return re.sub(r"^[0-9]\s+", "", value, count=1)
    if answer[:1].isdigit() != option[:1].isdigit():
        if without_blank_marker(answer) == option or answer == without_blank_marker(option):
            return 1.0
    score = SequenceMatcher(None, answer, option, autojunk=False).ratio()
    if answer in option and len(option) - len(answer) <= 10:
        score = max(score, 0.97)
    answer_digits, option_digits = re.findall(r"\d+", answer), re.findall(r"\d+", option)
    if answer_digits and answer_digits != option_digits:
        score *= 0.6
    return score


def live_question_text(question: object) -> str:
    """Read the wording from the API's documented and alternate live fields."""
    if not isinstance(question, dict):
        return ""
    for key in ("question_text", "text", "content", "body", "title"):
        value = question.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def live_option_text(option: object) -> str:
    if not isinstance(option, dict):
        return ""
    for key in ("text", "content", "label", "option_text"):
        value = option.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass(frozen=True)
class Answer:
    level: int
    kind: str
    qnum: int
    question: str
    answer: str
    question_norm: str
    answer_norm: str


class AnswerBank:
    """Exact-first matching of the supplied financial answer set."""

    def __init__(self, rows: list[Answer]):
        self.rows = rows
        self.exact: dict[str, list[Answer]] = {}
        for row in rows:
            self.exact.setdefault(row.question_norm, []).append(row)

    @classmethod
    def load(cls, path: Path = ANSWERS) -> "AnswerBank":
        path = Path(path)
        rows = []

        def add(level: int, kind: str, item: dict, source: str) -> None:
            if not isinstance(item, dict):
                raise FinancialApiError(f"تنسيق إجابة غير صالح في {source}.")
            question, answer = str(item.get("question") or "").strip(), str(item.get("answer") or "").strip()
            try:
                qnum = int(item.get("qnum"))
            except (TypeError, ValueError) as exc:
                raise FinancialApiError(f"رقم سؤال غير صالح في {source}.") from exc
            if not question or not answer:
                raise FinancialApiError(f"سؤال أو إجابة فارغة في {source}.")
            rows.append(Answer(level, kind, qnum, question, answer, norm(question), norm(answer)))

        if path.is_dir():
            try:
                index = json.loads((path / "index.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise FinancialApiError("فهرس ملفات إجابات الثقافة المالية مفقود أو تالف.") from exc
            if not isinstance(index, list) or not index:
                raise FinancialApiError("فهرس ملفات إجابات الثقافة المالية فارغ أو غير صالح.")
            levels = set()
            for record in index:
                if not isinstance(record, dict):
                    raise FinancialApiError("يوجد بند غير صالح في فهرس إجابات الثقافة المالية.")
                try:
                    level = int(record["level"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise FinancialApiError("فهرس إجابات الثقافة المالية يحتوي مستوى غير صالح.") from exc
                file_name = str(record.get("file") or "")
                if not file_name or Path(file_name).name != file_name or not file_name.endswith(".json"):
                    raise FinancialApiError(f"اسم ملف غير صالح للمستوى المالي {level}.")
                if level in levels:
                    raise FinancialApiError(f"المستوى المالي {level} مكرر في فهرس الإجابات.")
                levels.add(level)
                try:
                    source = json.loads((path / file_name).read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise FinancialApiError(f"ملف إجابات المستوى المالي {level} مفقود أو تالف.") from exc
                if not isinstance(source, dict) or int(source.get("level") or 0) != level:
                    raise FinancialApiError(f"ملف إجابات المستوى المالي {level} لا يطابق الفهرس.")
                for key, kind, count_key in (("lesson", "lesson", "lesson_count"),
                                             ("exam", "exam", "exam_count")):
                    items = source.get(key)
                    expected = record.get(count_key)
                    if not isinstance(items, list) or not isinstance(expected, int) or len(items) != expected:
                        raise FinancialApiError(f"عدد إجابات {kind} غير صالح في المستوى المالي {level}.")
                    for item in items:
                        add(level, kind, item, f"المستوى {level} ({key})")
            if levels != set(range(1, MAX_LEVEL + 1)):
                raise FinancialApiError("فهرس ملفات إجابات الثقافة المالية لا يحتوي المستويات 1 إلى 22 كاملة.")
            # Keep the flat source as a recovery layer. It is normally a
            # duplicate of the lesson files, but it may contain a newer or
            # differently grouped record than the live curriculum metadata.
            source_file = path.parent / "answers.json"
            if source_file.is_file():
                try:
                    source_rows = json.loads(source_file.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise FinancialApiError("المصدر الأصلي financial/answers.json مفقود أو تالف.") from exc
                if not isinstance(source_rows, list):
                    raise FinancialApiError("المصدر الأصلي financial/answers.json لا يحمل قائمة صالحة.")
                known = {(row.question_norm, row.answer_norm) for row in rows}
                for item in source_rows:
                    if not isinstance(item, dict):
                        raise FinancialApiError("تنسيق إجابة غير صالح في financial/answers.json.")
                    level = int(item.get("level"))
                    question_norm = norm(item.get("question"))
                    answer_norm = norm(item.get("answer"))
                    if (question_norm, answer_norm) in known:
                        continue
                    add(level, str(item.get("kind") or "question"), item, str(source_file))
                    known.add((question_norm, answer_norm))
        else:
            # Compatibility path for the retired single-file bank.
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise FinancialApiError("بنك إجابات الثقافة المالية مفقود أو تالف.") from exc
            if not isinstance(raw, list):
                raise FinancialApiError("بنك إجابات الثقافة المالية لا يحمل قائمة صالحة.")
            for item in raw:
                if not isinstance(item, dict):
                    raise FinancialApiError("تنسيق إجابة غير صالح في بنك الثقافة المالية.")
                try:
                    level = int(item["level"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise FinancialApiError("مستوى غير صالح في بنك الثقافة المالية.") from exc
                add(level, str(item.get("kind") or "question"), item, str(path))
        if not rows:
            raise FinancialApiError("بنك إجابات الثقافة المالية فارغ أو تالف.")
        return cls(rows)

    def find(self, question: str, options: list[dict], level: int) -> tuple[Answer, float]:
        q_norm = norm(question)
        if not q_norm:
            raise FinancialApiError("الخادم لم يرسل نص سؤال الثقافة المالية بصيغة مقروءة.")
        # The same wording can legitimately occur in a lesson and an exam,
        # or in different curriculum levels, with different answer wording.
        # Prefer the current live level before declaring a contradiction.
        all_exact = self.exact.get(q_norm, [])
        scoped_exact = [row for row in all_exact if row.level == level]
        exact = scoped_exact or all_exact
        if exact:
            answers = {row.answer_norm for row in exact}
            if len(answers) == 1:
                return exact[0], 1.0
            # If lesson/exam wording still collides within the same level,
            # let the live options identify the one exact model answer.
            exact_option_rows = [
                row for row in exact
                if any(option_score(row.answer_norm, norm(live_option_text(opt))) == 1.0
                       for opt in options)
            ]
            distinct_option_answers = {row.answer_norm for row in exact_option_rows}
            if len(distinct_option_answers) == 1:
                return exact_option_rows[0], 1.0
            raise FinancialApiError("توجد إجابات نموذجية مختلفة لنفس نص سؤال الثقافة المالية.")

        visible = norm(" ".join([question] + [live_option_text(opt) for opt in options])).split()

        def rank(rows):
            ranked = []
            for row in rows:
                score = 0.65 * coverage(row.question_norm.split(), visible)
                score += 0.35 * coverage(row.answer_norm.split(), visible)
                ranked.append((score, row))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            return ranked

        scoped = [row for row in self.rows if row.level == level]
        ranked = rank(scoped or self.rows)
        # If the curriculum returns a unit in an unexpected order, a global
        # near-exact prompt/option match is safer than incorrectly declaring
        # that the question has no answer in the assumed level.
        if ranked and ranked[0][0] < 0.90 and scoped:
            global_ranked = rank(self.rows)
            if global_ranked and global_ranked[0][0] >= 0.97:
                ranked = global_ranked
        if not ranked or ranked[0][0] < 0.90:
            # A few platform responses paraphrase the question wording while
            # retaining an exact visible option.  Use this narrow fallback
            # only when the bank produces one and only one exact option answer
            # and its question shares meaningful wording with the live prompt.
            prompt_words = norm(question).split()
            exact_option_rows = []
            candidate_rows = scoped or self.rows
            for row in candidate_rows:
                option_match = max((option_score(row.answer_norm, norm(live_option_text(opt)))
                                    for opt in options), default=0.0)
                question_match = coverage(row.question_norm.split(), prompt_words)
                if option_match == 1.0 and question_match >= 0.20:
                    exact_option_rows.append(row)
            # The original flat source can contain the exact item under a
            # different curriculum level than the live chapter metadata. A
            # single exact answer-option match plus meaningful prompt overlap
            # is safe to use globally; never choose by level alone.
            if not exact_option_rows and scoped:
                for row in self.rows:
                    option_match = max((option_score(row.answer_norm, norm(live_option_text(opt)))
                                        for opt in options), default=0.0)
                    question_match = coverage(row.question_norm.split(), prompt_words)
                    if option_match == 1.0 and question_match >= 0.50:
                        exact_option_rows.append(row)
            distinct_answers = {row.answer_norm for row in exact_option_rows}
            if len(distinct_answers) == 1:
                return exact_option_rows[0], 0.98
            best = ranked[0][0] if ranked else 0.0
            raise FinancialApiError(f"لا توجد إجابة مالية مطابقة بثقة للسؤال الحالي (أفضل تطابق {best:.2f}).")
        best_score, best = ranked[0]
        for score, other in ranked[1:]:
            if score > best_score - 0.03 and other.answer_norm != best.answer_norm:
                raise FinancialApiError("سؤالان ماليان متقاربان بإجابتين مختلفتين؛ أوقفت التشغيل بدل التخمين.")
        return best, best_score

    @staticmethod
    def option_id(answer: Answer, options: list[dict]) -> int:
        choices = [(option_score(answer.answer_norm, norm(live_option_text(opt))), opt) for opt in options]
        choices.sort(key=lambda pair: pair[0], reverse=True)
        if not choices or choices[0][0] < 0.90:
            raise FinancialApiError("لم أجد خيارًا حيًا يطابق الإجابة النموذجية بدقة.")
        best_score, best = choices[0]
        exact = [(score, opt) for score, opt in choices if score == 1.0]
        if exact:
            # The reviewed answer bank is authoritative. Once the complete
            # normalized answer is present among the live options, similar
            # distractors must not block the submission. If the API duplicated
            # that same visible answer, choose the stable lowest live id.
            exact.sort(key=lambda pair: int(pair[1].get("id", 2**63 - 1)))
            best_score, best = exact[0]
        if any(score > best_score - 0.05 and option_norm(live_option_text(opt)) != option_norm(live_option_text(best))
               for score, opt in choices[1:]):
            # Only approximate matches are ambiguous. An exact model-answer
            # match has already been resolved above.
            if not exact:
                raise FinancialApiError("خياران حيان متقاربان؛ لن أرسل تخمينًا.")
        try:
            return int(best["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FinancialApiError("الخادم لم يرسل معرّف خيار صالحًا.") from exc


class Client:
    def __init__(self, credentials: tuple[str, str], session: requests.Session | None = None):
        self.login_id, self.password = credentials
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Language": "ar,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
        })

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Referer": BASE + "/subjects/" + str(SUBJECT_ID)}
        if method != "GET":
            headers["Origin"] = BASE
        try:
            response = self.session.request(method, BASE + path, json=payload, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise FinancialApiError(f"تعذر الاتصال بخادم الثقافة المالية: {exc}") from exc
        if response.status_code in (401, 403):
            raise FinancialApiError(
                f"خادم الثقافة المالية رفض الطلب (HTTP {response.status_code}). "
                "تحقق من الاتصال وبيانات الدخول؛ لم تُرسل أي إجابة لاحقة."
            )
        if not response.ok:
            raise FinancialApiError(
                f"خادم الثقافة المالية أعاد HTTP {response.status_code} للمسار {path}.",
                status_code=response.status_code,
                path=path,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FinancialApiError("خادم الثقافة المالية أعاد صفحة غير صالحة بدل بيانات API.") from exc
        if not isinstance(body, dict) or body.get("success") is not True:
            raise FinancialApiError(f"خادم الثقافة المالية رفض العملية في {path}.")
        return body.get("data") if "data" in body else {}

    def login(self) -> None:
        data = self.request("POST", "/api/v1/auth/login", {"login_id": self.login_id, "password": self.password})
        if not isinstance(data, dict) or data.get("id") is None:
            raise FinancialApiError("تسجيل الدخول لم يُعد بيانات الطالب المتوقعة.")
        me = self.request("GET", "/api/v1/auth/me")
        if not isinstance(me, dict) or me.get("id") is None:
            raise FinancialApiError("لم يتم تأكيد جلسة الطالب بعد تسجيل الدخول.")

    def curriculum(self) -> list[dict]:
        data = self.request("GET", f"/api/v1/subjects/{SUBJECT_ID}/curriculum")
        if not isinstance(data, list):
            raise FinancialApiError("رد المنهج لا يحتوي على وحدات صالحة.")
        return data


def unit_for_level(curriculum: list[dict], level: int) -> dict | None:
    """Resolve a level without trusting one unstable curriculum field."""
    if not isinstance(curriculum, list) or not isinstance(level, int):
        return None
    # Prefer an explicit level identifier when the API supplies one.
    for unit in curriculum:
        if not isinstance(unit, dict):
            continue
        for key in ("level", "level_number", "level_id", "number"):
            try:
                if int(unit.get(key)) == level:
                    return unit
            except (TypeError, ValueError):
                continue
    # unit_order is useful only when it identifies exactly one unit.
    ordered = []
    for unit in curriculum:
        if not isinstance(unit, dict):
            continue
        try:
            if int(unit.get("unit_order")) == level:
                ordered.append(unit)
        except (TypeError, ValueError):
            continue
    if len(ordered) == 1:
        return ordered[0]
    # The platform sometimes returns units in their learning order while the
    # `unit_order` fields are incomplete/non-consecutive.  The reviewed bank
    # explicitly maps lessons 1..22, so the returned server order is the safe
    # fallback and avoids a false "level 7 not found" stop.
    if 1 <= level <= len(curriculum) and isinstance(curriculum[level - 1], dict):
        return curriculum[level - 1]
    return None


def is_done(item: dict) -> bool:
    return item.get("is_completed") is True


def ordered_items(unit: dict) -> list[dict]:
    """Keep curriculum section order, then item order within each section.

    The API resets ``item_order`` to 1 for every section. Sorting the whole
    unit by that field alone incorrectly produced 70, 71, 72 ... and skipped
    the review/test items that must follow chapter 70 before chapter 71.
    """
    raw = [row for row in unit.get("items") or [] if isinstance(row, dict)]
    section_order = {}
    for row in raw:
        section = str(row.get("section_id"))
        section_order.setdefault(section, len(section_order))
    return sorted(raw, key=lambda row: (
        section_order[str(row.get("section_id"))],
        int(row.get("item_order") or 0),
    ))


def unlock(client: Client) -> None:
    # Observed after a completed chapter in the supplied HAR.  It is idempotent
    # and refreshes the platform's next-item availability.
    client.request("POST", f"/api/v1/subjects/{SUBJECT_ID}/achievements/unlock", {})


def solve_chapter(client: Client, bank: AnswerBank, level: int, item: dict, preview: bool) -> None:
    chapter_id = int(item["id"])
    data = client.request("GET", f"/api/v1/chapters/{chapter_id}/blocks")
    chapter = data.get("chapter") if isinstance(data, dict) else None
    blocks = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(chapter, dict) or not isinstance(blocks, list):
        raise FinancialApiError("رد درس الثقافة المالية لا يحتوي على الفقرات المتوقعة.")
    if chapter.get("is_completed") is True:
        print(f"[SKIP-DONE] L{level} chapter {chapter_id}")
        return
    cleared = {int(value) for value in chapter.get("cleared_block_ids") or [] if str(value).isdigit()}
    questions = [block for block in blocks if isinstance(block, dict) and block.get("type") == "question"]
    for block in questions:
        if int(block.get("id") or -1) in cleared:
            print(f"[ANSWER-SKIP] L{level} chapter Q{block.get('question_id')} — الخادم يؤكد إكمالها.")
            continue
        question, options = block.get("question"), block.get("options")
        if not isinstance(question, dict) or not isinstance(options, list):
            raise FinancialApiError("سؤال درس الثقافة المالية لا يحمل الخيارات المتوقعة.")
        wording = live_question_text(question)
        try:
            answer, score = bank.find(wording, options, level)
        except FinancialApiError:
            core.save_json(f"financial_L{level}_chapter_{chapter_id}_unresolved_{block.get('question_id') or block.get('id')}.json", {
                "level": level, "chapter_id": chapter_id, "block_id": block.get("id"),
                "question_id": block.get("question_id"), "question": wording,
                "options": [{"id": opt.get("id"), "text": live_option_text(opt)}
                            for opt in options if isinstance(opt, dict)],
            })
            raise
        option_id = bank.option_id(answer, options)
        if preview:
            print(f"[ANSWER-PREVIEW] L{level} lesson Q{answer.qnum} — {score:.2f}")
            continue
        result = client.request("POST", f"/api/v1/chapters/{chapter_id}/answer",
                                {"block_id": int(block["id"]), "selected_option_id": option_id, "hint_used": 0})
        if not isinstance(result, dict) or result.get("is_correct") is not True:
            raise FinancialApiError("الخادم لم يؤكد صحة إجابة الدرس؛ توقفت قبل متابعة أي سؤال.")
        print(f"[ANSWER-SENT] L{level} lesson Q{answer.qnum} — {score:.2f} exact_api")
    if not preview:
        complete = client.request("POST", f"/api/v1/chapters/{chapter_id}/complete", {})
        if not isinstance(complete, dict) or complete.get("completed") is not True:
            raise FinancialApiError("الخادم لم يؤكد اكتمال الدرس.")


def solve_review(client: Client, level: int, item: dict, preview: bool) -> None:
    review_id = int(item["id"])
    data = client.request("GET", f"/api/v1/reviews/{review_id}/items")
    review = data.get("review") if isinstance(data, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(review, dict) or not isinstance(items, list):
        raise FinancialApiError("رد مراجعة الثقافة المالية غير صالح.")
    if review.get("is_completed") is True:
        print(f"[SKIP-DONE] L{level} review {review_id}")
        return
    # HAR shows review items as explanation cards only; if the platform changes
    # them into questions, stop rather than falsely marking an assessment done.
    if any(isinstance(row, dict) and ("question" in row or "options" in row) for row in items):
        raise FinancialApiError("المراجعة تحتوي أسئلة بصيغة جديدة غير موثقة؛ لم يتم تجاوزها.")
    if preview:
        print(f"[REVIEW-PREVIEW] L{level} review {review_id} — {len(items)} بطاقة")
        return
    complete = client.request("POST", f"/api/v1/reviews/{review_id}/complete", {})
    if not isinstance(complete, dict) or complete.get("completed") is not True:
        raise FinancialApiError("الخادم لم يؤكد اكتمال المراجعة.")
    print(f"[REVIEW-COMPLETE] L{level} review {review_id} — {len(items)} بطاقة")


def solve_test(client: Client, bank: AnswerBank, level: int, item: dict, preview: bool) -> None:
    test_id = int(item["id"])
    history = client.request("GET", f"/api/v1/tests/{test_id}/history")
    if isinstance(history, list) and any(str(row.get("status", "")).lower() in {"submitted", "completed"}
                                         for row in history if isinstance(row, dict)):
        print(f"[SKIP-DONE] L{level} test {test_id}")
        return
    data = client.request("POST", f"/api/v1/tests/{test_id}/start", {})
    if not isinstance(data, dict):
        raise FinancialApiError("رد بدء الاختبار غير صالح.")
    questions, options, test_questions = data.get("questions"), data.get("options"), data.get("test_questions")
    responses = data.get("question_responses")
    if not isinstance(questions, dict) or not isinstance(options, dict) or not isinstance(test_questions, list) or not isinstance(responses, list):
        raise FinancialApiError("رد الاختبار لا يحتوي على الأسئلة المتوقعة.")
    # Different API deployments have used either test_question_id or question_id
    # as the response lookup key. Keep both aliases; the answer endpoint is
    # resolved below from IDs returned by the same live response.
    response_by_question = {}
    for row in responses:
        if not isinstance(row, dict):
            continue
        for key in ("test_question_id", "question_id"):
            value = row.get(key)
            if value is not None:
                response_by_question.setdefault(str(value), row)
    for test_question in sorted((row for row in test_questions if isinstance(row, dict)),
                                key=lambda row: int(row.get("sort_order") or 0)):
        tqid, qid = str(test_question.get("id")), str(test_question.get("question_id"))
        response = response_by_question.get(tqid)
        question, choices = questions.get(qid), options.get(qid)
        if not isinstance(response, dict) or not isinstance(question, dict) or not isinstance(choices, list):
            raise FinancialApiError("أحد أسئلة الاختبار لا يملك بيانات مطابقة.")
        if response.get("selected_option_id") is not None or str(response.get("status", "")).lower() in {"answered", "submitted"}:
            print(f"[ANSWER-SKIP] L{level} test Q{qid} — الخادم يؤكد الإجابة السابقة.")
            continue
        wording = live_question_text(question)
        try:
            answer, score = bank.find(wording, choices, level)
        except FinancialApiError:
            core.save_json(f"financial_L{level}_test_{test_id}_unresolved_{qid}.json", {
                "level": level, "test_id": test_id, "question_id": qid, "question": wording,
                "options": [{"id": opt.get("id"), "text": live_option_text(opt)}
                            for opt in choices if isinstance(opt, dict)],
            })
            raise
        option_id = bank.option_id(answer, choices)
        if preview:
            print(f"[ANSWER-PREVIEW] L{level} test Q{answer.qnum} — {score:.2f}")
            continue
        saved = save_test_answer(client, test_id, test_question, response, option_id)
        if not isinstance(saved, dict) or saved.get("saved") is not True:
            raise FinancialApiError("الخادم لم يؤكد حفظ إجابة الاختبار.")
        print(f"[ANSWER-SENT] L{level} test Q{answer.qnum} — {score:.2f} exact_api")
    if not preview:
        submitted = client.request("POST", f"/api/v1/tests/{test_id}/submit", {})
        response = submitted.get("response") if isinstance(submitted, dict) else None
        if not isinstance(response, dict) or str(response.get("status", "")).lower() not in {"submitted", "completed"}:
            raise FinancialApiError("الخادم لم يؤكد تسليم الاختبار.")
        print(f"[TEST-COMPLETE] L{level} test {test_id}")


def save_test_answer(client: Client, test_id: int, test_question: dict,
                     response: dict, option_id: int) -> dict:
    """Save one test answer across the platform's observed route variants.

    The normal contract remains the first attempt (PUT + response id). Some
    accounts expose the endpoint with the test-question/question id instead,
    or accept POST rather than PUT. We only try those live IDs after a 404;
    no fallback is attempted for auth, validation, or server errors.
    """
    payload = {"selected_option_id": option_id}
    ids = []
    for value in (response.get("id"), response.get("test_question_id"),
                  test_question.get("id"), response.get("question_id"),
                  test_question.get("question_id")):
        if value is not None and str(value).isdigit() and str(value) not in ids:
            ids.append(str(value))
    if not ids:
        raise FinancialApiError("الخادم لم يرسل معرّفًا صالحًا لإجابة سؤال الاختبار.")

    candidates = [("PUT", ids[0])]
    # Preserve the original request as the only normal path. The remaining
    # combinations are compatibility fallbacks and are reached only on 404.
    for method in ("POST", "PUT"):
        for value in ids[1:]:
            candidates.append((method, value))
    last_error = None
    for index, (method, value) in enumerate(candidates):
        path = f"/api/v1/tests/{test_id}/questions/{value}/answer"
        try:
            return client.request(method, path, payload)
        except FinancialApiError as exc:
            last_error = exc
            if exc.status_code != 404 or index == len(candidates) - 1:
                raise
    raise last_error or FinancialApiError("تعذر حفظ إجابة الاختبار.")


def solve_item(client: Client, bank: AnswerBank, level: int, item: dict, preview: bool) -> None:
    kind = str(item.get("item_type") or "").lower()
    if kind == "chapter":
        solve_chapter(client, bank, level, item, preview)
    elif kind == "review":
        solve_review(client, level, item, preview)
    elif kind == "test":
        solve_test(client, bank, level, item, preview)
    else:
        raise FinancialApiError(f"نوع عنصر مالي غير معروف: {kind or 'فارغ'}.")


def run(max_level: int, credentials: tuple[str, str], stop_event, preview: bool = False) -> int:
    client, bank = Client(credentials), AnswerBank.load()
    core.RUN_STATE["chapter"], core.RUN_STATE["results"] = None, []
    print("[FINANCE-API] تشغيل مباشر عبر API الثقافة المالية.")
    try:
        client.login()
        print("[LOGIN] تم تأكيد جلسة طالب الثقافة المالية من الخادم.")
        # Read one consistent curriculum snapshot. Re-fetching it for every
        # level made a transient/incomplete response look like “level 7 is
        # missing” even though the level existed in the original response.
        curriculum = client.curriculum()
        for level in range(1, min(int(max_level), MAX_LEVEL) + 1):
            if stop_event.is_set():
                print("[STOP-REQUESTED] أوقفت التشغيل بطلبك.")
                return 2
            unit = unit_for_level(curriculum, level)
            if unit is None:
                raise FinancialApiError(f"لم أجد المستوى المالي {level} في المنهج.")
            items = ordered_items(unit)
            if not items:
                raise FinancialApiError(f"المستوى المالي {level} لا يحتوي عناصر قابلة للحل.")
            core.RUN_STATE["chapter"] = f"L{level}"
            print(f"[CHAPTER] L{level} | عناصر: {len(items)}")
            for item in items:
                if stop_event.is_set():
                    print("[STOP-REQUESTED] أوقفت التشغيل بطلبك.")
                    return 2
                if is_done(item):
                    print(f"[SKIP-DONE] L{level} {item.get('item_type')} {item.get('id')}")
                    # A completed item can still leave the next item's
                    # is_unlocked flag stale in the curriculum snapshot. The
                    # unlock endpoint is the platform's normal idempotent
                    # refresh; call it only after server-reported completion.
                    if not preview:
                        unlock(client)
                    continue
                if item.get("is_unlocked") is False:
                    # Refresh the curriculum after unlocking completed
                    # predecessors. Proceed only when the refreshed server
                    # record explicitly says this exact item is unlocked.
                    if not preview:
                        target_id = str(item.get("id"))
                        for attempt in range(1, 4):
                            unlock(client)
                            # The unlock request is asynchronous on some
                            # accounts. Give the server a short window, then
                            # read the exact item again; never bypass a false
                            # is_unlocked flag locally.
                            time.sleep(0.5 * attempt)
                            refreshed = unit_for_level(client.curriculum(), level)
                            refreshed_item = next(
                                (row for row in (refreshed or {}).get("items") or []
                                 if isinstance(row, dict) and str(row.get("id")) == target_id),
                                None,
                            )
                            if refreshed_item is not None:
                                item = refreshed_item
                            if item.get("is_unlocked") is not False:
                                print(f"[UNLOCK-CONFIRMED] L{level} item {target_id} (attempt {attempt})")
                                break
                            print(f"[UNLOCK-WAIT] L{level} item {target_id} ({attempt}/3)")
                    if item.get("is_unlocked") is False:
                        core.save_json(f"financial_L{level}_locked_{item.get('id')}.json", {
                            "level": level,
                            "item": item,
                            "reason": "server_keeps_is_unlocked_false_after_refresh",
                            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                        })
                        raise FinancialApiError(f"العنصر التالي في L{level} ما زال مقفلًا على الخادم؛ لن أتخطاه.")
                solve_item(client, bank, level, item, preview)
                if not preview:
                    unlock(client)
            core.RUN_STATE["results"].append({"chapter_id": f"L{level}",
                                               "assessment": {"assessment_completed": True,
                                                              "status": "level_finished"}})
            print(f"[CHAPTER-COMPLETE] L{level}")
        print("[FINISHED] اكتمل تشغيل الثقافة المالية.")
        return 0
    except FinancialApiError as exc:
        current = core.RUN_STATE.get("chapter")
        if current:
            core.RUN_STATE["results"].append({"chapter_id": current,
                                               "assessment": {"assessment_completed": False,
                                                              "status": "level_incomplete",
                                                              "stop_reason": "api_or_answer_error"}})
        print(f"[ERROR] {exc}")
        return 1
