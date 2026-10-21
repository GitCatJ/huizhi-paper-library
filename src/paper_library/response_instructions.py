"""Literal task instructions shared by parsing and AI input safety checks."""
from __future__ import annotations

import re


def unquoted_text(text: str) -> str:
    """Mask quoted examples without changing offsets into the original source."""
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"'}
    stack: list[str] = []
    result = []
    for char in text:
        if stack and char == stack[-1]:
            stack.pop()
            result.append(" ")
        elif char in pairs:
            stack.append(pairs[char])
            result.append(" ")
        else:
            result.append(" " if stack else char)
    return "".join(result)


_CHOICE_INSTRUCTIONS = (
    ("indeterminate", r"不定项(?:选择)?(?:题)?"),
    ("multiple", r"多项选择(?:题)?|多选题"),
    ("single", r"单项选择(?:题)?|单选题|"
     r"(?:备选答案|选项)[^。！？\n]{0,100}?只有[一1][个项][^。！？\n]{0,30}?(?:符合|正确)|"
     r"(?:备选答案|选项)[^。！？\n]{0,80}?选(?:出|择)(?:最恰当的)?[一1][个项]"),
    ("unknown", r"(?:备选答案|选项)[^。！？\n]{0,100}?选(?:出|择)(?:正确|最恰当)(?:的)?(?:选项|答案)"),
)


def choice_instruction_evidence(text: str) -> list[dict]:
    """Unknown cardinality still proves selection; never derive it from answers."""
    visible = unquoted_text(text)
    evidence = []
    for mode, pattern in _CHOICE_INSTRUCTIONS:
        for match in re.finditer(pattern, visible):
            start, end = match.span()
            evidence.append({"mode": mode, "start": start, "end": end,
                             "quote": text[start:end]})
    return sorted(evidence, key=lambda item: (item["start"], item["end"], item["mode"]))


def choice_instruction_modes(text: str) -> set[str]:
    return {item["mode"] for item in choice_instruction_evidence(text)
            if item["mode"] != "unknown"}


def requires_written_response(stem: str) -> bool:
    """Only direct task language, not a mention of writing inside a choice."""
    visible = unquoted_text(stem)
    return bool(
        "分条作答" in visible
        or re.search(r"谈谈.{0,40}(?:看法|认识|理解|启示)", visible)
        or re.search(r"(?:指出|找出).{0,60}(?:论证|公文|写作|语言).{0,60}"
                     r"(?:错误|问题).{0,80}(?:说明|理由|改正)", visible)
        or re.search(r"(?:^|[。！？.!?\n])\s*(?:请)?(?:说明|解释)(?:其|选择的)?(?:理由|原因)\s*[。！？.!?]?\s*$", visible)
        or re.search(r"(?:并|且|还需)(?:分别)?(?:说明|解释)(?:其|选择的)?(?:理由|原因)"
                     r"\s*[。！？.!?]?\s*(?:要求[:：].*)?$", visible)
        or re.search(r"(?:^|[。！？.!?\n])\s*(?:请)?(?:简述|简要说明|简要回答|试举例论述|试论述|撰写)", visible)
    )
