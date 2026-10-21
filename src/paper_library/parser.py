from __future__ import annotations

import copy
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from lxml import etree, html

from .richtext import content_from_elements, content_from_nodes, text_slice
from .question_quality import apply_question_quality_flags, infer_question_profile
from .utils import normalize_display_text, normalize_match_text, stable_id


PARSER_VERSION = "huizhi-standalone-html-v1"
OPTION_RE = re.compile(r"^\s*([A-H])\s*[、,，.．:：]\s*(.*)$", re.DOTALL | re.IGNORECASE)
OPTION_PREFIX_RE = re.compile(r"^\s*[A-H]\s*[、,，.．:：]\s*", re.IGNORECASE)
INLINE_OPTION_RE = re.compile(r"(?:^|\s)([A-H])\s*[、,，.．:：]\s*", re.IGNORECASE)
ANSWER_RE = re.compile(r"^\s*(\d+)\s*[、,，.．:：]\s*([A-H]+)", re.IGNORECASE)
CHINESE_NO_RE = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、.．]\s*(.*)$", re.DOTALL)
CHINESE_PAREN_RE = re.compile(r"^\s*[（(]([一二三四五六七八九十]+)[）)]")
TRIAL_MARKER_RE = re.compile(r"^\s*[【\[]\s*试题\s*([一二三四五六七八九十0-9]+)\s*[】\]]\s*$")
MATERIAL_LABEL_RE = re.compile(r"^\s*(?:给定)?(材料|资料)\s*([0-9一二三四五六七八九十]+)?")
REFERENCE_RE = re.compile(r"(?:根据|结合)[^。！？]{0,100}?(?:材料|资料)[^。！？]{0,80}")
POINTS_RE = re.compile(r"[（(]\s*\d+(?:\.\d+)?\s*分\s*[）)]")


def class_tokens(element: etree._Element) -> set[str]:
    return set((element.get("class") or "").split())


def has_class(element: etree._Element, name: str) -> bool:
    return name in class_tokens(element)


def element_text(element: etree._Element) -> str:
    return normalize_display_text(" ".join(element.itertext()))


def load_html_document(path: Path) -> etree._Element:
    parser = html.HTMLParser(encoding="utf-8", recover=True)
    return html.fromstring(path.read_bytes(), parser=parser)


def find_main_container(document: etree._Element) -> etree._Element:
    bodies = document.xpath("//body")
    body = bodies[0] if bodies else document
    candidates = [
        child
        for child in body
        if isinstance(child.tag, str) and child.tag.lower() == "div" and not has_class(child, "footer")
    ]
    if not candidates:
        return body
    return max(candidates, key=lambda item: len(etree.tostring(item, encoding="utf-8")))


def iter_with_class(root: etree._Element, name: str) -> list[etree._Element]:
    return [element for element in root.iter() if isinstance(element.tag, str) and has_class(element, name)]


def numbered_row_parts(row: etree._Element) -> tuple[int, etree._Element] | None:
    lefts = [child for child in row if isinstance(child.tag, str) and has_class(child, "left")]
    rights = [child for child in row if isinstance(child.tag, str) and has_class(child, "right")]
    if not lefts or not rights:
        return None
    number_text = element_text(lefts[0])
    if not number_text.isdigit():
        return None
    return int(number_text), rights[0]


def extract_options(right: etree._Element, question_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    clone = copy.deepcopy(right)
    options: list[dict[str, Any]] = []
    candidates: list[etree._Element] = []
    for element in clone.iterdescendants():
        if not isinstance(element.tag, str) or element.tag.lower() not in {"div", "p"}:
            continue
        match = OPTION_RE.match(element_text(element))
        if match:
            candidates.append(element)

    # Legacy exports mix <p> and <div> options in a single question. Select
    # the innermost option blocks so a layout wrapper cannot swallow A-D.
    candidate_set = set(candidates)
    candidates = [element for element in candidates
                  if not any(child in candidate_set for child in element.iterdescendants())]
    # A stem may itself begin with "A、B两站...". A later block with the
    # same label is the option; retain the earlier paragraph as stem evidence.
    last_by_label = {OPTION_RE.match(element_text(e)).group(1).upper(): e for e in candidates}
    candidates = [e for e in candidates
                  if last_by_label[OPTION_RE.match(element_text(e)).group(1).upper()] is e]
    if any(len(list(INLINE_OPTION_RE.finditer(element_text(element)))) > 1
           for element in candidates):
        return _extract_inline_options(
            content_from_elements([clone], owner_id=question_id, role="stem"), question_id
        )

    seen_labels: set[str] = set()
    for element in candidates:
        match = OPTION_RE.match(element_text(element))
        if not match:
            continue
        label = match.group(1).upper()
        if label in seen_labels:
            continue
        seen_labels.add(label)
        option_id = stable_id("option", question_id, label)
        content = content_from_elements([element], owner_id=option_id, role=f"option.{label}")
        if content["nodes"] and content["nodes"][0].get("type") == "text":
            node=content['nodes'][0]; prefix=OPTION_PREFIX_RE.match(node['text'])
            start=prefix.end() if prefix else 0
            while start<len(node['text']) and node['text'][start].isspace():start+=1
            end=len(node['text'].rstrip())
            content['nodes'][0]=text_slice(node,start,end)
            content["plain_text"] = OPTION_PREFIX_RE.sub("", content["plain_text"], count=1).strip()
            content["normalized_text"] = normalize_match_text(content["plain_text"])
        options.append({"id": option_id, "label": label, "ordinal": len(options) + 1, "content": content})

        parent = element.getparent()
        if parent is not None:
            parent.remove(element)

    stem = content_from_elements([clone], owner_id=question_id, role="stem")
    if not options:
        stem, options = _extract_inline_options(stem, question_id)
    elif _extract_inline_options(stem, question_id)[1]:
        # Some files put A/B after the stem but C/D in separate blocks.
        # Re-split the original stream to preserve the complete source order.
        stem, options = _extract_inline_options(
            content_from_elements([right], owner_id=question_id, role="stem"), question_id
        )
    return stem, options


def _extract_inline_options(
    content: dict[str, Any], question_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split legacy paragraphs containing ``stem A、... B、...``.

    Splitting the node stream rather than plain text keeps images attached to
    the option in which they originally appeared.
    """
    stem_nodes: list[dict[str, Any]] = []
    option_nodes: dict[str, list[dict[str, Any]]] = {}
    option_order: list[str] = []
    active_label: str | None = None

    def append_node(node: dict[str, Any]) -> None:
        target = stem_nodes if active_label is None else option_nodes[active_label]
        if node.get("type") == "text" and not node.get("text"):
            return
        target.append(node)

    for source_node in content.get("nodes", []):
        if source_node.get("type") != "text":
            append_node(copy.deepcopy(source_node))
            continue
        text = source_node.get("text", "")
        matches = list(INLINE_OPTION_RE.finditer(text))
        if not matches:
            append_node(copy.deepcopy(source_node))
            continue
        cursor = 0
        for index, match in enumerate(matches):
            append_node(text_slice(source_node,cursor,match.start()))
            label = match.group(1).upper()
            if label not in option_nodes:
                option_nodes[label] = []
                option_order.append(label)
            active_label = label
            cursor = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            append_node(text_slice(source_node,cursor,end))
            cursor = end

    # Avoid treating an incidental "A、" as an option boundary.
    if len(option_order) < 2 or option_order[0] != "A":
        return content, []

    stem = content_from_nodes(stem_nodes, owner_id=question_id, role="stem")
    options: list[dict[str, Any]] = []
    for ordinal, label in enumerate(option_order, start=1):
        option_id = stable_id("option", question_id, label)
        options.append(
            {
                "id": option_id,
                "label": label,
                "ordinal": ordinal,
                "content": content_from_nodes(
                    option_nodes[label], owner_id=option_id, role=f"option.{label}"
                ),
            }
        )
    return stem, options


def parse_answer_map(document: etree._Element) -> dict[int, list[str]]:
    result: dict[int, list[str]] = defaultdict(list)
    for element in iter_with_class(document, "col-xs-1-5"):
        match = ANSWER_RE.match(element_text(element))
        if match:
            result[int(match.group(1))].append(match.group(2).upper())
    if result:
        return result

    for text in document.itertext():
        match = ANSWER_RE.match(normalize_display_text(text))
        if match:
            result[int(match.group(1))].append(match.group(2).upper())
    return result


def parse_objective_analysis(document: etree._Element, paper_id: str) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in iter_with_class(document, "row"):
        parts = numbered_row_parts(row)
        if not parts:
            continue
        number, right = parts
        owner = stable_id("explanation", paper_id, number, len(result[number]) + 1)
        result[number].append(content_from_elements([right], owner_id=owner, role="explanation"))
    return result


def _material_scope(text: str, materials: list[dict[str, Any]]) -> dict[str, Any]:
    from .material_references import numbered_scope, REF, NUM
    from .material_references import references_material
    from .material_structure import is_placeholder
    materials = [m for m in materials if not is_placeholder(m['content'])]
    if not materials and references_material(text):
        return {'scope_type':'unresolved','raw_expression':'引用材料但本卷未解析出材料','material_ids':[],'confidence':0.0}
    all_markers = ("全部材料", "全部资料", "所有材料", "所有资料", "全部给定材料", "全部给定资料")
    all_marker = next((marker for marker in all_markers if marker in text), None)
    if all_marker:
        return {'scope_type':'all_materials','raw_expression':all_marker,
                'material_ids':[material['id'] for material in materials],'confidence':0.8}
    numbered = numbered_scope(text, materials)
    # A quoted item can be followed by an instruction to use all given material.
    # Do not narrow that instruction to the quoted item's number.
    covered=[m.span() for m in REF.finditer(text)]
    generic=[m for m in re.finditer(r'给定\s*(?:材料|资料)',text)
             if not any(a<=m.start()<b for a,b in covered)
             and not re.match(rf'\s*[（(]\s*(?:材料|资料)\s*{NUM}',text[m.end():])]
    if numbered is not None and generic and len(materials)>1:
        return {'scope_type':'all_materials','raw_expression':'给定材料/资料',
                'material_ids':[material['id'] for material in materials],'confidence':0.7}
    if numbered is not None:
        return numbered
    evidence_match = REFERENCE_RE.search(text)
    evidence = evidence_match.group(0) if evidence_match else ""
    if any(marker in text for marker in ('以上材料','以上资料','上述材料','上述资料')) or (evidence and len(materials) > 1):
        return {
            "scope_type": "all_materials",
            "raw_expression": evidence,
            "material_ids": [material["id"] for material in materials],
            "confidence": 0.8,
        }
    if any(marker in text for marker in ("给定材料", "给定资料")) and len(materials) > 1:
        return {
            "scope_type": "all_materials",
            "raw_expression": "给定材料/资料",
            "material_ids": [material["id"] for material in materials],
            "confidence": 0.7,
        }

    inferred = _infer_material_by_lexical_anchor(text, materials)
    if inferred:
        return inferred
    if len(materials) == 1:
        return {
            "scope_type": "implicit_single",
            "raw_expression": evidence,
            "material_ids": [materials[0]["id"]],
            "confidence": 0.75,
        }
    return {
        "scope_type": "unresolved",
        "raw_expression": evidence,
        "material_ids": [],
        "confidence": 0.0,
    }


def _infer_material_by_lexical_anchor(
    text: str, materials: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Infer an implicit material from unique names and quoted phrases.

    This is intentionally conservative: only n-grams present in exactly one
    material vote, and the winner must clearly dominate the runner-up. It
    resolves prompts such as "推介临诗村" without pretending every vague
    prompt has a deterministic scope.
    """
    if len(materials) < 2:
        return None
    query = normalize_match_text(text)
    material_texts = [material["content"].get("normalized_text", "") for material in materials]
    scores = [0] * len(materials)
    evidence: list[set[str]] = [set() for _ in materials]
    for size in range(8, 2, -1):
        grams = {query[index : index + size] for index in range(max(0, len(query) - size + 1))}
        # Stable iteration is essential: the evidence cap otherwise picks a
        # different subset for each PYTHONHASHSEED and changes frozen AI input.
        for gram in sorted(grams):
            owners = [index for index, content in enumerate(material_texts) if gram in content]
            if len(owners) == 1:
                owner = owners[0]
                scores[owner] += size * size
                if len(evidence[owner]) < 8:
                    evidence[owner].add(gram)
    ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
    best_index, best_score = ranked[0]
    runner_up = ranked[1][1]
    if best_score < 80 or best_score < max(1, runner_up) * 1.8:
        return None
    return {
        "scope_type": "lexical_inferred",
        "raw_expression": "、".join(sorted(evidence[best_index], key=lambda s: (-len(s), s))[:5]),
        "material_ids": [materials[best_index]["id"]],
        "confidence": min(0.9, 0.65 + best_score / 2000),
    }


def parse_objective_truth(document: etree._Element, paper: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    container = find_main_container(document)
    questions: list[dict[str, Any]] = []
    materials: list[dict[str, Any]] = []
    section_title = ""
    active_material_id: str | None = None

    rows = [child for child in container if isinstance(child.tag, str) and has_class(child, "row")]
    if not rows:
        rows = iter_with_class(container, "row")

    for row in rows:
        subtitle_nodes = [element for element in row.iter() if isinstance(element.tag, str) and has_class(element, "subtitle")]
        sub2_nodes = [element for element in row.iter() if isinstance(element.tag, str) and has_class(element, "sub2title")]
        parts = numbered_row_parts(row)

        if subtitle_nodes and not parts:
            section_title = element_text(subtitle_nodes[0])
            active_material_id = None
            continue

        if sub2_nodes and not parts:
            label = element_text(sub2_nodes[0]) or f"资料组{len(materials) + 1}"
            material_id = stable_id("material", paper["id"], len(materials) + 1, label)
            materials.append(
                {
                    "id": material_id,
                    "paper_id": paper["id"],
                    "label": label,
                    "ordinal": len(materials) + 1,
                    "section_title": section_title,
                    "content": content_from_elements([row], owner_id=material_id, role=f"material.{len(materials) + 1}"),
                }
            )
            active_material_id = material_id
            continue

        if parts:
            number, right = parts
            occurrence_id = stable_id("question", paper["id"], number, len(questions) + 1)
            stem, options = extract_options(right, occurrence_id)
            question_kind, response_mode = infer_question_profile(
                stem_text=stem.get("plain_text", ""),
                section_title=section_title,
                options=options,
                default_kind="objective",
            )
            questions.append(
                {
                    "id": occurrence_id,
                    "paper_id": paper["id"],
                    "source_no": number,
                    "ordinal": len(questions) + 1,
                    "section_title": section_title,
                    "question_kind": question_kind,
                    "response_mode": response_mode,
                    "stem": stem,
                    "options": options,
                    "material_scope": {
                        "scope_type": "layout_inferred" if active_material_id else "none",
                        "raw_expression": "",
                        "material_ids": [active_material_id] if active_material_id else [],
                        "confidence": 0.9 if active_material_id else 1.0,
                    },
                    "answer_claims": [],
                    "explanations": [],
                    "quality_flags": [],
                }
            )
            continue

        if "资料分析" in section_title and any(element.tag.lower() == "img" for element in row.iter() if isinstance(element.tag, str)):
            material_id = stable_id("material", paper["id"], len(materials) + 1, "implicit")
            materials.append(
                {
                    "id": material_id,
                    "paper_id": paper["id"],
                    "label": f"资料组{len(materials) + 1}",
                    "ordinal": len(materials) + 1,
                    "section_title": section_title,
                    "content": content_from_elements([row], owner_id=material_id, role=f"material.{len(materials) + 1}"),
                }
            )
            active_material_id = material_id

    return questions, materials


def _flush_material(
    paper: dict[str, Any],
    materials: list[dict[str, Any]],
    label: str,
    elements: list[etree._Element],
) -> None:
    if not elements:
        return
    material_id = stable_id("material", paper["id"], len(materials) + 1, label)
    materials.append(
        {
            "id": material_id,
            "paper_id": paper["id"],
            "label": label or f"材料{len(materials) + 1}",
            "ordinal": len(materials) + 1,
            "section_title": "给定材料",
            "content": content_from_elements(elements, owner_id=material_id, role=f"material.{len(materials) + 1}"),
        }
    )


def parse_subjective_truth(document: etree._Element, paper: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    container = find_main_container(document)
    materials: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    material_elements: list[etree._Element] = []
    material_label = ""
    question_elements: list[etree._Element] = []
    question_label = ""
    question_has_requirements = False
    mode = "preamble"

    def flush_question() -> None:
        nonlocal question_elements, question_label, question_has_requirements
        if not question_elements:
            return
        ordinal = len(questions) + 1
        question_id = stable_id("question", paper["id"], "subjective", ordinal)
        wrapper = etree.Element("div")
        for element in question_elements:
            wrapper.append(copy.deepcopy(element))
        stem, options = extract_options(wrapper, question_id)
        question_kind, response_mode = infer_question_profile(
            stem_text=stem["plain_text"],
            section_title="作答要求",
            options=options,
            default_kind="subjective",
        )
        questions.append(
            {
                "id": question_id,
                "paper_id": paper["id"],
                "source_no": ordinal,
                "source_label": question_label or str(ordinal),
                "ordinal": ordinal,
                "section_title": "作答要求",
                "question_kind": question_kind,
                "response_mode": response_mode,
                "stem": stem,
                "options": options,
                "material_scope": {},
                "answer_claims": [],
                "explanations": [],
                "quality_flags": [],
            }
        )
        question_elements = []
        question_label = ""
        question_has_requirements = False

    children = [child for child in container if isinstance(child.tag, str)]
    for element in children:
        tag = element.tag.lower()
        text = element_text(element)
        compact = normalize_match_text(text)

        if tag == "h2" and ("给定材料" in compact or "给定资料" in compact):
            mode = "materials"
            continue
        if tag == "h2" and ("作答要求" in compact or "回答问题" in compact):
            _flush_material(paper, materials, material_label, material_elements)
            material_elements = []
            material_label = ""
            mode = "questions"
            continue

        if mode == "materials":
            label_match = MATERIAL_LABEL_RE.match(text)
            if label_match and len(text) <= 60:
                _flush_material(paper, materials, material_label, material_elements)
                material_elements = []
                suffix = label_match.group(2) or str(len(materials) + 1)
                material_label = f"材料{suffix}"
            material_elements.append(copy.deepcopy(element))
            continue

        if mode == "questions":
            marker = ""
            marker_text = text
            # Some exports put the delimiter outside the heading's inline tag:
            # <b>二</b>、<p>下一题...</p>. itertext() excludes the element's own
            # tail, but rich-text copying preserves it. Recognize this boundary
            # before appending the heading to the preceding question. Only a
            # pure numeral plus punctuation qualifies; never trim body content.
            if (tag in {"b", "strong", "span"}
                    and re.fullmatch(r"[一二三四五六七八九十]+", text)
                    and re.fullmatch(r"\s*[、.．]\s*", element.tail or "")
                    and not element.xpath(".//img")):
                marker_text = text + element.tail
            if has_class(element, "bs-callout") and "试题" in text:
                marker = text
            else:
                chinese_match = CHINESE_NO_RE.match(marker_text)
                if chinese_match:
                    marker = chinese_match.group(1)
                else:
                    parenthesized = CHINESE_PAREN_RE.match(text)
                    trial = TRIAL_MARKER_RE.match(text)
                    if parenthesized:
                        marker = parenthesized.group(1)
                    elif trial:
                        marker = trial.group(1)
            if marker:
                flush_question()
                question_label = marker
            elif question_elements and question_has_requirements and POINTS_RE.search(text):
                # Many 申论 papers omit 一、二、 markers. A new scored prompt after
                # the previous "要求" block is a reliable boundary.
                flush_question()
            question_elements.append(copy.deepcopy(element))
            if text.lstrip().startswith("要求"):
                question_has_requirements = True

    if material_elements:
        _flush_material(paper, materials, material_label, material_elements)
    flush_question()

    if not questions:
        owner = stable_id("question", paper["id"], "subjective", 1)
        questions.append(
            {
                "id": owner,
                "paper_id": paper["id"],
                "source_no": 1,
                "source_label": "未拆分",
                "ordinal": 1,
                "section_title": "作答要求",
                "question_kind": "subjective",
                "response_mode": "constructed_response",
                "stem": content_from_elements(children, owner_id=owner, role="stem"),
                "options": [],
                "material_scope": {},
                "answer_claims": [],
                "explanations": [],
                "quality_flags": ["subjective_question_boundary_unresolved"],
            }
        )

    for question in questions:
        question["material_scope"] = _material_scope(question["stem"]["plain_text"], materials)
        if question['material_scope']['scope_type']=='unresolved' and question['material_scope']['raw_expression']:
            question['quality_flags'].append('material_scope_unresolved')
    return questions, materials


def parse_subjective_solutions(document: etree._Element, paper_id: str) -> list[dict[str, Any]]:
    container = find_main_container(document)
    groups: list[list[etree._Element]] = []
    current: list[etree._Element] = []
    for element in [child for child in container if isinstance(child.tag, str)]:
        text = element_text(element)
        boundary = (has_class(element, "bs-callout") and "试题" in text) or (
            element.tag.lower() == "blockquote" and "参考答案" in text
        )
        if boundary and current:
            groups.append(current)
            current = []
        current.append(copy.deepcopy(element))
    if current:
        groups.append(current)
    return [
        content_from_elements(group, owner_id=stable_id("solution", paper_id, index), role="explanation")
        for index, group in enumerate(groups, start=1)
    ]


def _option_answer_content(question: dict[str, Any], answer: str) -> list[str]:
    option_map = {option["label"]: option["content"]["normalized_text"] for option in question["options"]}
    return [option_map[label] for label in answer if label in option_map]


def parse_paper(sample: dict[str, Any]) -> dict[str, Any]:
    snapshots = sample.get('_source_snapshots', {})
    document_paths = {kind: snapshots[kind]['archive_path'] if kind in snapshots else path
                      for kind, path in sample['components'].items()}
    paper = {
        "id": sample["id"],
        "year": sample["year"],
        "region": sample["region"],
        "exam_type": sample["exam"],
        "category": sample["category"],
        "title": sample["title"],
        "base_name": sample["base_name"],
        "parser_version": PARSER_VERSION,
        "components": [],
        "quality_flags": [],
    }
    source_files: list[dict[str, Any]] = []
    for kind, raw_path in sample["components"].items():
        path = Path(raw_path)
        source_files.append(
            {
                "id": stable_id("source", str(path.resolve())),
                "paper_id": paper["id"],
                "kind": kind,
                "path": str(path.resolve()),
                "size_bytes": snapshots[kind]['size_bytes'] if kind in snapshots else path.stat().st_size,
            }
        )
        if kind in snapshots:
            from .source_archive import verify_source
            source_files[-1].update(sha256=snapshots[kind]['sha256'], archive=snapshots[kind])
            verify_source(source_files[-1])
        paper["components"].append({"kind": kind, "source_file_id": source_files[-1]["id"]})

    truth_document = load_html_document(Path(document_paths["真题"]))
    standard_rows = sum(1 for row in iter_with_class(truth_document, "row") if numbered_row_parts(row))
    is_subjective = sample["category"] in {"申论", "综合应用"} or standard_rows == 0
    if is_subjective:
        questions, materials = parse_subjective_truth(truth_document, paper)
    else:
        questions, materials = parse_objective_truth(truth_document, paper)

    answer_map: dict[int, list[str]] = {}
    analysis_map: dict[int, list[dict[str, Any]]] = {}
    subjective_solutions: list[dict[str, Any]] = []
    if "答案" in sample["components"]:
        answer_map = parse_answer_map(load_html_document(Path(document_paths["答案"])))
    if "解析" in sample["components"]:
        analysis_document = load_html_document(Path(document_paths["解析"]))
        if is_subjective:
            subjective_solutions = parse_subjective_solutions(analysis_document, paper["id"])
        else:
            analysis_map = parse_objective_analysis(analysis_document, paper["id"])

    for question in questions:
        apply_question_quality_flags(question)
        is_placeholder = "placeholder_question" in question["quality_flags"]
        number = question["source_no"]
        for answer in answer_map.get(number, []):
            question["answer_claims"].append(
                {
                    "id": stable_id("answer", question["id"], answer),
                    "source_paper_id": paper["id"],
                    "source_question_no": number,
                    "answer_value": answer,
                    "answer_option_contents": _option_answer_content(question, answer),
                    "status": "source_claim",
                }
            )
        question["explanations"].extend(analysis_map.get(number, []))
        if is_subjective and number <= len(subjective_solutions):
            question["explanations"].append(subjective_solutions[number - 1])

        question_kind, response_mode = infer_question_profile(
            stem_text=question["stem"].get("plain_text", ""),
            section_title=question.get("section_title", ""),
            options=question.get("options", []),
            default_kind=question["question_kind"],
            answer_values=[
                claim.get("answer_value", "")
                for claim in question.get("answer_claims", [])
            ],
            explanation_texts=[
                explanation.get("plain_text", "")
                for explanation in question.get("explanations", [])
            ],
        )
        question["question_kind"] = question_kind
        question["response_mode"] = response_mode

        if (
            question["response_mode"] == "choice"
            and not question["options"]
            and not is_placeholder
        ):
            question["quality_flags"].append("options_not_parsed")
        if question["question_kind"] == "objective" and not question["answer_claims"] and not is_placeholder:
            question["quality_flags"].append("answer_missing")

    if not questions:
        paper["quality_flags"].append("no_questions_parsed")
    if standard_rows == 0 and sample["category"] not in {"申论", "综合应用"}:
        paper["quality_flags"].append("category_structure_mismatch")
    if any("placeholder_question" in question["quality_flags"] for question in questions):
        paper["quality_flags"].append("contains_placeholder_questions")

    paper["question_count"] = len(questions)
    paper["material_count"] = len(materials)
    return {
        "paper": paper,
        "source_files": source_files,
        "questions": questions,
        "materials": materials,
    }
