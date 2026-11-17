# -*- coding: utf-8 -*-
"""بنك إجابات نصي (ملفات الفصول المنسّقة) يُطابَق بنص السؤال الحي وقت التشغيل.

الملفات مكتوبة للقراءة البشرية: لا توجد أرقام أسئلة حقيقية. لذلك نقرأ كل فصل إلى «بنود» (سؤال + كتل كود الإجابة)
ثم نطابق كل سؤال حي ببند واحد فقط بتشابه النص، ونشتق الإجابة بقواعد صارمة. أي غموض → لا إجابة (تتوقف الأداة
عند السؤال وتكتب تشخيصًا) ولا يتم التخمين.
"""
import difflib
import json
import re
import textwrap
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
_ANSWER = re.compile(r"^\*{0,2}\s*(?:الاجابة|الإجابة|الاجابه|الإجابه)\s*[:：]?\s*\*{0,2}\s*[:：]?\s*(.*)$")
# Source sheets use several human phrases to introduce the whole editor
# buffer.  Treating only "الكود الكامل" as final lost otherwise explicit
# solutions such as "ليصبح الكود هكذا" and made the runner send a single
# placeholder fragment instead of the completed program.
_FINAL = re.compile(r"النهائي|النهايي|الكامل|كامل|(?:ليصبح|ليكون).{0,24}الكود")
_FITB = re.compile(r"fitbBlock\d+")
# Display wrappers and terminal browser notes can lower an otherwise exact
# reviewed-question match.  Ambiguous neighbours remain rejected below.
MIN_SIMILARITY = 0.75
MIN_MARGIN = 0.03


def norm(s):
    s = unicodedata.normalize("NFKC", str(s or "").lower())
    s = re.sub(r"\[\[/?(?:bdi|bold|italic|underline|copy)[^\]]*\]\]", " ", s)
    # QUREO sometimes appends this display-only sentence to the live prompt
    # although it is absent from the supplied answer source.  It conveys no
    # question-specific meaning, so removing only this exact boilerplate keeps
    # an otherwise identical question matchable without lowering the global
    # similarity threshold.
    s = re.sub(r"لن يتم عرض أي شيء في المتصفح عند التنفيذ\.?", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^\w\u0600-\u06ff]+", " ", s)).strip()


def choice_key(value):
    """Normalize a live option without destroying HTML/code choices.

    QUREO wraps some choices in ``[[bdi]]`` markers.  Those markers are
    presentation-only, while literal tags such as ``<img ...>`` and ``<p>``
    are the actual answer and must remain part of the comparison.
    """
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"\[\[/?(?:bdi|bold|italic|underline|copy|code)[^\]]*\]\]", "", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def choice_structure_key(value):
    """Normalize display wrappers while preserving meaningful code line breaks."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = re.sub(r"\[\[/?(?:bdi|bold|italic|underline|copy|code)[^\]]*\]\]", "", value)
    lines = [re.sub(r"[ \t]+", " ", line).strip().casefold()
             for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def live_simple_python_calculation(q):
    """Derive only an explicit two-number calculation from a live Python prompt.

    Some supplied source records swap commutative operands (for example 40+19
    vs the live 19+40).  When QUREO explicitly names one simple expression and
    the live editor is exactly ``print()``, its literal expression is the
    safest source of truth.  This deliberately does not handle variables,
    prose, or arbitrary code.
    """
    if str(q.get("type") or "").lower() == "choice" or q.get("choices"):
        return None
    starters = [str(item.get("code") or "") for item in (q.get("descriptions") or [])
                if isinstance(item, dict)]
    if len(starters) != 1 or re.sub(r"\s+", "", starters[0]) != "print()":
        return None
    prompt = unicodedata.normalize("NFKC", str(q.get("content") or ""))
    if not ("يحسب" in prompt and "يطبع" in prompt):
        return None
    expressions = re.findall(r"(?<!\d)(\d+)\s*([+\-xX×÷/])\s*(\d+)(?!\d)", prompt)
    if len(expressions) != 1:
        return None
    left, op, right = expressions[0]
    op = {"x": "*", "X": "*", "×": "*", "÷": "/"}.get(op, op)
    return {"kind": "description", "answer_code": f"print({left}{op}{right})",
            "source_status": "live_explicit_calculation"}


def _clean_code(code):
    code = code.replace("\u3000", "  ").replace("\r", "")
    lines = [ln.rstrip() for ln in code.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _fragment(code):
    """جزء يُكتب في فراغ: بدون مسافات زائدة وبدون فاصلة منقوطة أخيرة (كما في الأداة السابقة)."""
    cleaned = textwrap.dedent(_clean_code(code))
    return cleaned.rstrip(";").strip("\n") if "\n" in cleaned else cleaned.strip().rstrip(";")


def parse_chapter(text):
    """نص ملف فصل → قائمة بنود: {num, text, norm, blocks:[{code,label,final}], prose}."""
    entries, cur, mode, fence = [], None, "q", None

    def close():
        nonlocal cur
        if cur is not None and cur["a_parts"]:
            q_text = "\n".join(p for _, p in cur["q_parts"]).strip()
            blocks, label = [], []
            prose_all = []
            for kind, value in cur["a_parts"]:
                if kind == "prose":
                    label.append(value)
                    prose_all.append(value)
                else:
                    joined = " ".join(label)
                    blocks.append({"code": _clean_code(value), "label": joined, "final": bool(_FINAL.search(joined))})
                    label = []
            if q_text and (blocks or prose_all):
                entries.append({"num": cur["num"], "text": q_text, "norm": norm(q_text), "blocks": blocks,
                                "prose": "\n".join(prose_all).strip()})
        cur = None

    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("```"):
            if fence is None:
                fence = []
            else:
                code, fence = "\n".join(fence), None
                if cur is not None and mode in ("q", "a"):
                    cur["q_parts" if mode == "q" else "a_parts"].append(("code", code))
            continue
        if fence is not None:
            fence.append(raw)
            continue
        if s.startswith("###"):
            close()
            cur, mode = {"num": s.lstrip("# ").strip(), "q_parts": [], "a_parts": []}, "q"
            continue
        if re.match(r"^#{1,2}\s", s):
            close()
            continue
        if s == "---" or s.startswith("حل مراجعة") or s.startswith("حل المستوي"):
            close()
            mode = "q"
            continue
        if cur is None:
            continue
        m = _ANSWER.match(s)
        if m and mode == "q":
            mode = "a"
            if m.group(1).strip():
                cur["a_parts"].append(("prose", m.group(1).strip()))
            continue
        if s:
            cur["q_parts" if mode == "q" else "a_parts"].append(("prose", s))
    close()
    return entries


def parse_json_chapter(payload):
    """Structured JSON chapter -> the same safe entry format used for TXT files.

    The bundled JSON files preserve each question, prose instruction and code block
    separately.  Loading them directly avoids losing the corrected chapter mapping
    while keeping the conservative matching and answer-validation rules below.
    """
    if not isinstance(payload, dict):
        return []

    # Version 3 is the reviewed source-JSON import.  Its `final_answer` is
    # already structured as code replacement, ordered editor fragments, or a
    # visible choice.  Preserve that distinction instead of re-inferring it
    # from Arabic prose every time the tool starts.
    if payload.get("schema_version") == 3:
        entries = []
        for raw in payload.get("questions") or []:
            if not isinstance(raw, dict):
                return []
            prompt = str(raw.get("prompt") or "").strip()
            answer = raw.get("answer")
            if not prompt or not isinstance(answer, dict):
                return []
            mode = str(answer.get("mode") or "none")
            code = str(answer.get("code") or "").strip()
            fragments = [str(value).strip() for value in (answer.get("fragments") or [])
                         if str(value).strip()]
            blocks = []
            if mode in {"code_replace", "code_fill"}:
                blocks.extend({"code": fragment, "label": str(answer.get("where") or ""), "final": False}
                              for fragment in fragments)
                if code:
                    blocks.append({"code": code, "label": "final_answer", "final": True})
            elif mode in {"choice", "text"} and code:
                blocks.append({"code": code, "label": "final_answer", "final": False})
            elif mode not in {"none", "free"}:
                return []
            entries.append({
                "num": str(raw.get("source_question_heading") or raw.get("id") or ""),
                "text": prompt,
                "norm": norm(prompt),
                "blocks": blocks,
                "prose": str(answer.get("where") or "").strip(),
                "answer_mode": mode,
                "source_id": str(raw.get("source_id") or raw.get("id") or ""),
            })
        return entries

    # Version 2 is the canonical machine-oriented export rebuilt directly
    # from the supplied TXT answer sheets.  It deliberately stores one flat
    # question record per source question: prompt, guidance and code blocks.
    # That avoids making runtime matching depend on presentation-only lesson
    # headings or Markdown nesting.
    if payload.get("schema_version") == 2:
        entries = []
        for raw in payload.get("questions") or []:
            if not isinstance(raw, dict):
                return []
            prompt = str(raw.get("prompt") or "").strip()
            answer = raw.get("answer")
            if not prompt or not isinstance(answer, dict):
                return []
            blocks = []
            for block in answer.get("code_blocks") or []:
                if not isinstance(block, dict):
                    return []
                code = _clean_code(str(block.get("code") or ""))
                if not code:
                    return []
                blocks.append({"code": code, "label": str(block.get("label") or ""),
                               "final": bool(block.get("is_complete_editor"))})
            prose = str(answer.get("guidance") or "").strip()
            if not blocks and not prose:
                return []
            entries.append({"num": str(raw.get("source_question_heading") or ""),
                            "text": prompt, "norm": norm(prompt), "blocks": blocks,
                            "prose": prose})
        return entries

    def parts(values):
        if not isinstance(values, list):
            values = [values]
        out = []
        for value in values:
            if not isinstance(value, dict):
                continue
            kind = value.get("type")
            if kind == "code":
                code = str(value.get("code") or "")
                if code.strip():
                    out.append(("code", code))
            elif kind == "text":
                text = str(value.get("text") or "").strip()
                if text:
                    out.append(("prose", text))
        return out

    def entry(number, question_parts, answer_parts):
        if not question_parts or not answer_parts:
            return None
        q_text = "\n".join(value for _, value in question_parts).strip()
        if not q_text:
            return None
        blocks, label, prose_all = [], [], []
        for kind, value in answer_parts:
            if kind == "prose":
                label.append(value)
                prose_all.append(value)
            else:
                joined = " ".join(label)
                blocks.append({"code": _clean_code(value), "label": joined,
                               "final": bool(_FINAL.search(joined))})
                label = []
        if not blocks and not prose_all:
            return None
        return {"num": str(number or ""), "text": q_text, "norm": norm(q_text),
                "blocks": blocks, "prose": "\n".join(prose_all).strip()}

    entries = []

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "question":
            content = node.get("content")
            answer = node.get("answer")
            # A few supplied Python JSON questions carry an explicit Arabic answer
            # label followed by code in ``content`` instead of ``answer``.  Split
            # that known serialized form so the prompt and code are never confused.
            if not answer and isinstance(content, list):
                marker = next((i for i, value in enumerate(content)
                               if isinstance(value, dict) and value.get("type") == "text" and
                               re.search(r"(?:الاجابة|الإجابة|الاجابه|الإجابه)",
                                         str(value.get("text") or ""), re.I)), None)
                if marker is not None and marker + 1 < len(content):
                    answer = content[marker:]
                    content = content[:marker]
            item = entry(node.get("heading"), parts(content), parts(answer))
            if item is not None:
                entries.append(item)
            return
        visit(node.get("children"))

    visit(payload.get("children"))
    return entries


def _similarity(a, b):
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 25 and short in long_:
        ratio = max(ratio, 0.95)
    return ratio


class TextBank:
    def __init__(self, chapters):
        self.chapters = chapters        # {seq: [entry, ...]}

    @classmethod
    def load(cls, track, root=None):
        folder = Path(root or HERE / "answers") / track
        chapters = {}
        # JSON is the canonical bundled source when it is present.  The human-readable
        # TXT files remain a fallback, so a damaged or missing JSON chapter never makes
        # the rest of the track unusable.
        json_folder = HERE / "answers_json" / track if root is None else None
        if json_folder and json_folder.is_dir():
            for f in sorted(json_folder.glob("chapter_*.json")):
                try:
                    payload = json.loads(f.read_text(encoding="utf-8"))
                    n = int(payload.get("chapter_number"))
                    if payload.get("track_id") != track:
                        raise ValueError("track_id mismatch")
                    parsed = parse_json_chapter(payload)
                    if not parsed:
                        raise ValueError("chapter has no answerable questions")
                    chapters[n] = parsed
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    # TXT below is intentionally retained as the safe fallback.
                    continue
        for f in sorted(folder.glob("chapter_*.txt")):
            n = int(re.search(r"(\d+)", f.stem).group(1))
            if n not in chapters:
                chapters[n] = parse_chapter(f.read_text(encoding="utf-8", errors="replace"))
        return cls(chapters)

    def info(self):
        return {"chapters": len(self.chapters), "entries": sum(len(v) for v in self.chapters.values())}

    @staticmethod
    def _live_code_compatibility(entry, q):
        """Small deterministic tie-breaker for reviewed JSON entries.

        Several source chapters repeat nearly identical wording (for example
        print Hello five versus seven times).  Prompt similarity alone puts
        both records inside the ambiguity margin.  The visible starter editor
        and literals in the prompt identify which one actually belongs to the
        live question.  This adjusts only reviewed source-JSON records; the
        legacy TXT matching semantics remain unchanged.
        """
        if not entry.get("source_id") or str(q.get("type") or "").lower() == "choice":
            return 0.0
        blocks = entry.get("blocks") or []
        if not blocks:
            return 0.0
        candidate = next((block["code"] for block in reversed(blocks) if block.get("final")),
                         blocks[-1]["code"])
        context = "\n".join([str(q.get("content") or "")] + [
            str(part.get("code") or "") for part in (q.get("descriptions") or []) if isinstance(part, dict)
        ])
        context_identifiers = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", context))
        candidate_identifiers = [value for value in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", candidate)
                                if value not in _JSON_CODE_WORDS]
        score = 0.0
        if candidate_identifiers:
            matched = sum(value in context_identifiers for value in set(candidate_identifiers))
            score += 0.04 * matched / len(set(candidate_identifiers))
            if matched == 0:
                score -= 0.04
        prompt_numbers = set(re.findall(r"\d+", str(q.get("content") or "")))
        candidate_numbers = set(re.findall(r"\d+", candidate))
        if prompt_numbers:
            score -= 0.07 * len(prompt_numbers - candidate_numbers) / len(prompt_numbers)
        return score

    # ------------------------------------------------------------------ lookup
    def lookup(self, seq, q):
        """(answer dict | None, reason). Never guesses."""
        direct = live_simple_python_calculation(q)
        if direct is not None:
            return direct, "live_explicit_python_calculation"
        entries = self.chapters.get(int(seq))
        if not entries:
            return None, "text_bank_has_no_chapter"
        live = norm(q.get("content"))
        scored = sorted(((_similarity(live, e["norm"]) + self._live_code_compatibility(e, q),
                         _similarity(live, e["norm"]), i) for i, e in enumerate(entries)), reverse=True)
        best, best_raw = scored[0][0], scored[0][1]
        # Some QUREO descriptions omit boilerplate that exists in the reviewed
        # answer file.  When normal prompt similarity is too low, allow one
        # deterministic fallback only if the live starter editor is preserved
        # verbatim in exactly one reviewed final program and the live prompt
        # shares at least two code identifiers with that source prompt.
        if best_raw < MIN_SIMILARITY and str(q.get("type") or "").lower() == "description":
            starter = "\n".join(str(d.get("code") or "") for d in (q.get("descriptions") or [])
                                  if isinstance(d, dict) and d.get("code")).strip()
            live_context_text = "\n".join([
                str(q.get("content") or ""),
                *[str(hint) for hint in (q.get("hints") or [])],
            ])
            live_ids = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", live_context_text))
            starter_matches = []
            if starter and len(live_ids) >= 2:
                compact = lambda value: re.sub(r"\s+", "", value)
                starter_compact = compact(starter)
                for entry in entries:
                    blocks = entry.get("blocks") or []
                    final = next((b.get("code") for b in reversed(blocks) if b.get("final")), None)
                    if not final or starter_compact not in compact(str(final)):
                        continue
                    source_ids = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", entry.get("text") or ""))
                    source_ids.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(final)))
                    if len(live_ids & source_ids) >= 2:
                        starter_matches.append(entry)
            if len(starter_matches) == 1:
                derived, reason = self._derive(starter_matches[0], q)
                if derived:
                    return derived, "text_bank_unique_starter_code_match"
        if best_raw < MIN_SIMILARITY:
            return None, "text_bank_no_question_match"
        close = [entries[i] for score, _, i in scored if score >= best - MIN_MARGIN]
        if any(entry.get("answer_mode") in {"none", "free"} for entry in close):
            return None, "text_bank_source_has_no_submit_answer"
        derived = [self._derive(e, q) for e in close]
        usable = [d for d in derived if d[0]]
        if not usable:
            return None, derived[0][1]
        first = usable[0][0]
        same = lambda a, b: (a["kind"], a.get("choice_id"), a.get("answer_code")) == \
            (b["kind"], b.get("choice_id"), b.get("answer_code"))
        if any(not same(first, u[0]) for u in usable[1:]):
            hint_scores = [(self._choice_hint_score(item[0], q), item) for item in usable]
            best_hint = max(score for score, _ in hint_scores)
            hinted = [item for score, item in hint_scores if score == best_hint]
            if best_hint > 0 and len(hinted) == 1:
                return hinted[0][0], "text_bank_choice_hint_match"
            return None, "text_bank_ambiguous_match"
        return first, usable[0][1]

    def _derive(self, entry, q):
        qtype = str(q.get("type") or "").lower()
        choices = q.get("choices") or []
        if qtype == "choice" or choices:
            return self._choice(entry, choices)
        return self._description(entry, q)

    @staticmethod
    def _choice_hint_score(answer, q):
        """Use explicit live hint wording only as a deterministic tie-breaker."""
        if not answer or answer.get("kind") != "choice":
            return 0
        text = " ".join([str(q.get("content") or ""),
                          *[str(x) for x in (q.get("hints") or [])]]).casefold()
        choice = str(answer.get("answer") or "").casefold()
        score = 0
        if re.search(r"\band\b|معا", text):
            score += int(bool(re.search(r"\band\b", choice)))
        if re.search(r"\bor\b|أو", text):
            score += int(bool(re.search(r"\bor\b", choice)))
        return score

    @staticmethod
    def _choice(entry, choices):
        blocks = entry["blocks"]
        if len(blocks) == 1:
            text = blocks[0]["code"]
        elif not blocks and entry["prose"]:
            text = entry["prose"].splitlines()[0]
        else:
            return None, "text_bank_choice_answer_unclear"
        target = choice_key(text)
        exact = [c for c in choices if target and choice_key(c.get("content")) == target and c.get("id") is not None]
        if len(exact) != 1:
            squeezed = re.sub(r"\s+", "", target)
            exact = [c for c in choices if squeezed and re.sub(r"\s+", "", choice_key(c.get("content"))) == squeezed
                     and c.get("id") is not None]
        if len(exact) != 1:
            structured = choice_structure_key(text)
            exact = [c for c in choices
                     if structured and choice_structure_key(c.get("content")) == structured
                     and c.get("id") is not None]
        if len(exact) != 1 and target.count("・"):
            # In Arabic RTL rendering QUREO may display the Japanese list dot
            # before each item while the supplied answer source serializes it
            # after each item.  Keep the dot count as part of the signature so
            # this cannot collapse into an otherwise identical plain list.
            target_count = target.count("・")
            target_words = re.sub(r"[・\s]+", "", target)
            exact = [c for c in choices
                     if choice_key(c.get("content")).count("・") == target_count
                     and re.sub(r"[・\s]+", "", choice_key(c.get("content"))) == target_words
                     and c.get("id") is not None]
        if not exact:
            # Some Python choices append a fixed editor scaffold such as
            # "الكود الخاص بك" after the actual first line. Match that line
            # only when it is the complete first non-empty line, so an invalid
            # near-match such as ``for i range(10):`` is not accepted.
            exact = []
            for c in choices:
                lines = [line.strip() for line in choice_structure_key(c.get("content")).splitlines()
                         if line.strip()]
                if lines and lines[0] == choice_structure_key(text) and c.get("id") is not None:
                    exact.append(c)
        if len(exact) == 1:
            return {"kind": "choice", "choice_id": int(exact[0]["id"]), "answer": text,
                    "source_status": "text_bank"}, "text_bank_choice_match"
        if len(exact) > 1:
            # QUREO occasionally emits duplicate-looking choices with different
            # ids. If the visible text is identical, use the first canonical
            # choice id; genuinely different choices remain unresolved.
            signatures = {choice_structure_key(c.get("content")) for c in exact}
            if len(signatures) == 1:
                chosen = min(exact, key=lambda c: int(c["id"]))
                return {"kind": "choice", "choice_id": int(chosen["id"]), "answer": text,
                        "source_status": "text_bank"}, "text_bank_duplicate_choice_lowest_id"
        return None, "text_bank_choice_not_mapped_uniquely"

    @staticmethod
    def _description(entry, q):
        starter = ""
        for d in q.get("descriptions") or []:
            if isinstance(d, dict) and d.get("code"):
                starter = str(d["code"])
                break
        blocks = entry["blocks"]
        finals = [b for b in blocks if b["final"]]
        others = [b for b in blocks if not b["final"]]
        slots = sorted(set(_FITB.findall(starter)), key=lambda s: int(s[9:]))
        if slots:                                         # ملء فراغات: الجواب هو ما يُكتب في الفراغات بالترتيب
            # Reviewed JSON can carry the known complete editor buffer in
            # addition to the fragments.  QUREO's API needs that full buffer,
            # not the individual missing expressions.
            if finals and entry.get("answer_mode") in {"code_fill", "code_replace"}:
                code = finals[-1]["code"]
                if not code or len(code) > 2000:
                    return None, "text_bank_description_answer_invalid_length"
                return {"kind": "description", "answer_code": code, "source_status": "text_bank"}, \
                    "text_bank_full_editor_from_json"
            fragments = [_fragment(b["code"]) for b in others]
            # Some answer sheets place a set of ordered one-line blanks inside
            # one Markdown code fence.  For example, three QUREO placeholders
            # are written as three lines under "بالترتيب".  Split only that
            # explicit form; multi-line code is never split speculatively.
            ordered_source = " ".join([entry.get("prose") or ""] +
                                       [str(block.get("label") or "") for block in others])
            if len(fragments) == 1 and len(slots) > 1 and "بالترتيب" in ordered_source:
                lines = [_fragment(line) for line in fragments[0].splitlines() if line.strip()]
                if len(lines) == len(slots) and all(lines):
                    fragments = lines
            if len(fragments) != len(slots):
                return None, "text_bank_fitb_fragment_count_mismatch"
            if any(not f for f in fragments):
                return None, "text_bank_fitb_empty_fragment"
            # The live QUREO requests in the supplied HAR send the complete edited
            # buffer, not just the fragments.  Rebuild that buffer from the server's
            # starter code and the uniquely checked fragments.
            filled = starter
            for slot, frag in zip(slots, fragments):
                filled = filled.replace(slot, frag)
            # Legacy TXT entries need a consistency check against any stated
            # final editor buffer.  Version 3 entries above already chose the
            # explicit final buffer before reaching this point.
            if finals:
                squash = lambda value: re.sub(r"[\s;\u3000]+", "", value)
                final_text = squash(finals[-1]["code"])
                if squash(filled) != final_text and not all(squash(f) in final_text for f in fragments):
                    return None, "text_bank_fitb_final_code_mismatch"
            code = filled
        elif finals:                                      # كود كامل يستبدل المحرر
            code = finals[-1]["code"]
        elif len(blocks) == 1:
            code = blocks[0]["code"]
        else:
            # A number of source TXT files split one full editor answer across
            # two adjacent Markdown fences: declarations in the first fence
            # and the rest of the program in the second.  The prose explicitly
            # says "مسح/تعديل الكود".  Reconstruct that editor only when the
            # live editor is non-empty and every quoted output literal is also
            # visible in the live prompt or starter code.  This keeps a typo
            # such as "Go to the store" from overriding the live "shop" task.
            labels = " ".join([entry.get("prose") or ""] +
                              [str(block.get("label") or "") for block in blocks])
            full_editor_hint = bool(re.search(r"(?:مسح|تعديل|نسخ).{0,100}الكود", labels, re.I))
            compact = lambda value: re.sub(r"\s+", "", value)
            last = blocks[-1]["code"]
            earlier = [block["code"] for block in blocks[:-1]]
            if full_editor_hint and starter:
                if earlier and all(compact(part) in compact(last) for part in earlier):
                    candidate = last
                else:
                    candidate = "\n".join(block["code"] for block in blocks)
                live_context = norm("\n".join([str(q.get("content") or ""), starter]))
                literals = [a or b for a, b in re.findall(
                    r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', candidate)]
                if all(not norm(value) or norm(value) in live_context for value in literals):
                    code = candidate
                else:
                    return None, "text_bank_full_editor_conflicts_with_live_prompt"
            else:
                return None, "text_bank_description_answer_unclear"
        if not code or len(code) > 2000:
            return None, "text_bank_description_answer_invalid_length"
        # Source files sometimes contain a translated/typoed literal.  A full
        # editor answer may be used only when every quoted output literal is
        # present in the live question or in its prefilled editor.  The exact
        # question-ID fallback can still provide a server-verified correction.
        if entry.get("source_id") and "<" not in code:
            live_context = norm("\n".join([str(q.get("content") or ""), starter]))
            source_context = norm(entry.get("text") or "")
            literals = [a or b for a, b in re.findall(
                r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'', code)]
            live_literals = [a or b for a, b in re.findall(
                r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'',
                "\n".join([str(q.get("content") or ""), starter]))]
            # Reject a source/liveness contradiction (e.g. source says
            # "store", but QUREO says "shop").  Some translated exercises
            # state the desired output only in Arabic, while their verified
            # editor solution necessarily contains the platform's English
            # literal; that is not a contradiction and may be used.
            if any(norm(value) and norm(value) not in live_context and (
                       norm(value) in source_context or
                       any(difflib.SequenceMatcher(None, norm(value), norm(live_value)).ratio() >= 0.80
                           for live_value in live_literals if norm(live_value)))
                   for value in literals):
                return None, "text_bank_full_editor_conflicts_with_live_prompt"
        if re.search(r"[\u0600-\u06ff]", code) and not re.search(r"[\"'`#/]", code):
            return None, "text_bank_description_answer_has_prose"
        return {"kind": "description", "answer_code": code, "source_status": "text_bank"}, \
            "text_bank_fitb_fragments" if slots else "text_bank_full_code"


_JSON_CODE_WORDS = {
    "let", "const", "var", "if", "else", "for", "while", "do", "function", "return",
    "true", "false", "null", "undefined", "console", "log", "break", "continue", "switch",
    "case", "default", "typeof", "new", "Math", "alert", "prompt", "length",
    "print", "range", "in", "and", "or", "not", "True", "False", "None",
    "input", "int", "str", "float", "len", "append", "class", "def", "from", "import",
}
