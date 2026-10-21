from __future__ import annotations

import re
import hashlib
from typing import Any

from .utils import normalize_match_text
from .response_instructions import choice_instruction_evidence, requires_written_response


PLACEHOLDER_EXACT = {
    "缺失",
    "题目缺失",
    "暂无",
    "暂无题目",
    "暂无题目内容",
    "题目暂无",
}
PLACEHOLDER_MARKERS = (
    "全力以赴征集",
    "题目正在征集",
    "暂未收录",
    "尚未收录",
    "待补充题目",
)


def is_placeholder_question(question: dict[str, Any]) -> bool:
    stem = normalize_match_text(
        str(question.get("stem", {}).get("normalized_text") or question.get("stem", {}).get("plain_text") or "")
    )
    if stem in PLACEHOLDER_EXACT:
        return True
    if re.fullmatch(r"题目缺失[,，、。:：;；!！]?全力搜集中[。.!！]?", stem):
        return True
    if any(marker in stem for marker in PLACEHOLDER_MARKERS):
        return True
    return False


def isolated_placeholder_fingerprint(content_fingerprint: str, occurrence_id: str) -> str:
    return hashlib.sha256(
        f"{content_fingerprint}\0placeholder\0{occurrence_id}".encode("utf-8")
    ).hexdigest()


def judgment_protocol_evidence(stem_text: str, options: list[dict[str, Any]]) -> dict | None:
    """Recognize explicit, source-local instructions; never infer an answer.

    Keep literal offsets so protocol revisions can bind the decision to archive
    text. Quoted classroom examples, missing choice options and a bare A/B
    answer are deliberately insufficient evidence.
    """
    if options:
        return None
    reason = re.search(
        r"(?:^|[。！？.!?\n])\s*(?P<instruction>请对(?:这一|这个|上述|该)"
        r"(?:观点|说法|表述)作出(?:判断和分析|判断并说明理由))[。！.!]?",
        stem_text,
    )
    if reason:
        start, end = reason.span("instruction")
        quote_stack = []
        pairs = {"“": "”", "‘": "’", "「": "」", "『": "』"}
        for char in stem_text[:start]:
            if quote_stack and char == quote_stack[-1]:
                quote_stack.pop()
            elif char in pairs:
                quote_stack.append(pairs[char])
            elif char == '"':
                quote_stack.append(char)
        if not quote_stack:
            return {"kind": "subjective", "response_mode": "judgment_reason",
                    "start": start, "end": end, "quote": stem_text[start:end],
                    "answer_symbols": None}
    # The heading must introduce this task, not occur inside quoted material.
    header = re.match(
        r"\s*(?:[一二三四五六七八九十\d]+\s*[、.．]\s*)?判断题\s*[:：]"
        r"(?P<instruction>[^。！？\n]{0,120}?正确的涂[“\"']?\s*(?P<true>[AB])\s*[”\"']?"
        r"\s*[,，、]\s*错误的涂[“\"']?\s*(?P<false>[AB])\s*[”\"']?)\s*[。.]",
        stem_text,
    )
    blanks = list(re.finditer(r"[（(]\s*[）)]", stem_text))
    if (header and header["true"] != header["false"] and len(blanks) == 1
            and re.fullmatch(r"\s*[。！？.!?]?\s*", stem_text[blanks[0].end():])
            and not re.search(r"说明(?:理由|原因)|分析(?:理由|原因)|分条作答", stem_text[header.end():])):
        start, end = header.span("instruction")
        return {"kind": "objective", "response_mode": "judgment",
                "start": start, "end": end, "quote": stem_text[start:end],
                "answer_symbols": {header["true"]: True, header["false"]: False}}
    return None


def infer_question_profile(
    *,
    stem_text: str,
    section_title: str,
    options: list[dict[str, Any]],
    default_kind: str,
    answer_values: list[str] | tuple[str, ...] = (),
    explanation_texts: list[str] | tuple[str, ...] = (),
) -> tuple[str, str]:
    """Infer the response protocol without treating examples as answer options.

    Strong task-language cues take precedence over extracted A/B nodes.  This
    matters for constructed-response prompts that contain an example written as
    ``A...B...`` and for mixed 综合应用 papers.  When answers and explanations are
    available, a deliberately strict evidence rule can also recover judgment
    questions whose source section was mislabeled as ordinary choice.
    """

    stem = normalize_match_text(stem_text)
    section = normalize_match_text(section_title)
    combined = f"{section}{stem}"

    explicit_judgment = judgment_protocol_evidence(stem_text, options)
    if explicit_judgment:
        return explicit_judgment["kind"], explicit_judgment["response_mode"]

    if re.search(
        r"(?:请)?判断下列.{0,80}(?:是否|说法).{0,160}(?:说明为什么|说明理由|分别说明)",
        stem,
    ) or (
        "打√" in stem and "打×" in stem and "说明" in stem
    ):
        return "subjective", "judgment_reason"

    if "填空题" in combined or re.search(
        r"分别找出.{0,120}(?:处|项).{0,80}最为类似", stem
    ):
        return "subjective", "fill_blank"

    if "作文" in section or re.search(
        r"(?:8\d{2}|9\d{2}|1\d{3})\s*[~～—至-]\s*\d{3,4}\s*字|不少于\s*\d{3}\s*字",
        stem_text,
    ):
        return "subjective", "essay"

    if requires_written_response(stem_text):
        return "subjective", "constructed_response"

    # Material-based case sections can contain ordinary single/multiple-choice
    # questions.  A real option list plus explicit choice language is stronger
    # evidence than the parent section label, so those questions must use the
    # objective protocol even inside 综合应用 or案例分析 sections.
    explicit_choice = bool(options) and bool(
        re.search(
            r"(?:单项选择|多项选择|下列.{0,80}(?:说法|表述|认定|选项|行为).{0,80}"
            r"(?:正确|错误|不正确|符合|不符合|恰当|不恰当|构成)|"
            r"(?:关于|根据).{0,100}(?:正确|错误|不正确|符合|不符合|恰当|不恰当)|"
            r".{0,60}行为构成)",
            stem,
        )
    )
    # A generic case heading is weaker than its explicit selection instructions.
    # Two distinct option labels are required for this new section-based route;
    # missing choices and quoted examples do not establish a choice protocol.
    labels = {str(option.get("label", "")).strip().upper() for option in options}
    section_choice = (len(options) >= 2 and len(labels) == len(options)
                      and all(re.fullmatch(r"[A-Z]", label) for label in labels)
                      and bool(choice_instruction_evidence(section_title)))
    if explicit_choice or section_choice:
        return "objective", "choice"

    # Section labels are reliable protocol hints. The same words inside an
    # objective stem are not: 行测 often asks which option supports a “论述”,
    # and ordinary choices can discuss “公文写作规范”.
    constructed_section = any(
        marker in section
        for marker in (
            "案例分析",
            "材料分析",
            "书写改错",
            "公文改错",
            "公文写作",
            "论述题",
            "主观题",
            "简答题",
            "简单题",
            "应用文改错",
        )
    ) or bool(re.search(r"(?:^|[一二三四五六七八九十、.])论述(?:[、。.：:]|$)", section))
    constructed = bool(
        constructed_section
        or (not options and re.match(r"^(?:请)?(?:简述|简要说明|简要回答|试举例论述|试论述)", stem))
    )
    if constructed:
        return "subjective", "constructed_response"

    # Some recalled papers put a whole true/false block below a stale single- or
    # multiple-choice heading.  Do not infer judgment from an empty option list
    # or an A/B answer alone: incomplete ordinary choices can look identical.
    # Reclassification is allowed only when the stem has a terminal judgment
    # blank, every source answer is A/B, and an explanation explicitly concludes
    # that the statement is correct or incorrect.
    normalized_answers = {
        normalize_match_text(str(value)).upper()
        for value in answer_values
        if normalize_match_text(str(value))
    }
    terminal_judgment_blank = bool(
        re.search(r"[（(]\s*[）)]\s*[。！？.!?]?\s*$", stem_text)
    )
    explicit_judgment_conclusion = any(
        re.search(
            r"(?:故|因此|所以)(?:本题|题干)?(?:表述|说法|观点|判断)?(?:是)?(?:正确|错误)$",
            normalize_match_text(str(value)),
        )
        for value in explanation_texts
    )
    if (
        not options
        and terminal_judgment_blank
        and normalized_answers
        and normalized_answers <= {"A", "B"}
        and explicit_judgment_conclusion
    ):
        return "objective", "judgment"

    if options:
        return "objective", "choice"
    if default_kind == "subjective":
        return "subjective", "constructed_response"
    if "判断" in section:
        return "objective", "judgment"
    return "objective", "choice"


def apply_question_quality_flags(question: dict[str, Any]) -> None:
    flags = list(dict.fromkeys(question.get("quality_flags", [])))
    if is_placeholder_question(question):
        for flag in ("placeholder_question", "dedupe_excluded"):
            if flag not in flags:
                flags.append(flag)
    question["quality_flags"] = flags
