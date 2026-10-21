from __future__ import annotations

from collections.abc import Iterable
import copy
import re
import unicodedata
from typing import Any

from lxml import etree

from .utils import normalize_display_text, normalize_match_text, stable_id


BLOCK_TAGS = {
    "address",
    "blockquote",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "p",
    "table",
    "tr",
}


def _tag_name(element: etree._Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _parse_dimension(value: str | None) -> int | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return int(digits) if digits else None


def _underline_space_chain(element: etree._Element) -> tuple[str, bool] | None:
    """Recognize plain <u> chains used by the source editor to draw a line.

    Attributes, other inline elements and explicit separators are deliberately
    excluded: two ordinary adjacent slots must keep their original identity.
    """
    depth = 0
    current = element
    while True:
        if _tag_name(current) != 'u' or current.attrib or len(current) > 1:
            return None
        depth += 1
        if not len(current):
            break
        current = current[0]
    raw = ''.join(element.itertext())
    if raw.strip() or any(ch in raw for ch in '\r\n\t'):
        return None
    return raw, depth > 1


def _fragmented_underline_run(children, start: int) -> tuple[int, str] | None:
    parts = []
    redundant = False
    end = start
    while end < len(children):
        part = _underline_space_chain(children[end])
        if part is None:
            break
        parts.append(part[0])
        redundant = redundant or part[1]
        end += 1
        # Even unmarked whitespace is an explicit boundary between slots.
        if children[end - 1].tail:
            break
    if redundant and sum(bool(p) for p in parts) > 1:
        return end, ''.join(parts)
    return None


def _punctuation_slot_parts(element: etree._Element, raw: str) -> list[str] | None:
    # A long decorated gap sometimes includes its closing quote. Restrict the
    # recovery to punctuation-only inline spans; spaces in words stay text.
    if (not raw.strip() or any(ch in raw for ch in '\r\n\t')
            or any(not ch.isspace() and not unicodedata.category(ch).startswith('P') for ch in raw)
            or any(_tag_name(e) not in {'u', 'span', 'b', 'strong', 'i', 'em'} for e in element.iter())):
        return None
    parts = re.split(r'([^\S\r\n\t]{2,})', raw)
    return parts if len(parts) > 1 else None


def content_from_elements(
    elements: Iterable[etree._Element],
    *,
    owner_id: str,
    role: str,
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    image_index = 0

    def add_text(value: str | None, underlined=False) -> None:
        if not value:
            return
        text = normalize_display_text(value)
        if not text:
            return
        if nodes and nodes[-1]["type"] == "text":
            start = len(nodes[-1]['text']) + 1
            nodes[-1]["text"] = normalize_display_text(nodes[-1]["text"] + " " + text)
        else:
            nodes.append({"type": "text", "text": text})
            start = 0
        if underlined:
            marks = nodes[-1].setdefault('marks', [])
            if marks and marks[-1]['end'] == start-1:
                marks[-1]['end'] = start+len(text)
                marks[-1]['quote'] = nodes[-1]['text'][marks[-1]['start']:marks[-1]['end']]
            else:
                marks.append({'kind':'underline','start':start,'end':start+len(text),'quote':text})

    def add_break() -> None:
        if nodes and nodes[-1]["type"] != "break":
            nodes.append({"type": "break"})

    def walk(element: etree._Element, underlined=False) -> None:
        nonlocal image_index
        tag = _tag_name(element)
        # lxml comments/processing instructions are not rendered elements and
        # may return None even for get(name, default). Their visible tail is
        # still appended by the parent, including any inherited underline.
        if not tag:
            return
        # CSS decoration extends over descendants, even if a child says none.
        declares_underline = tag == 'u' or bool(re.search(
            r'(?:^|;)\s*text-decoration(?:-line)?\s*:[^;]*\bunderline\b', element.get('style',''), re.I))
        underlined = underlined or declares_underline
        # A whitespace-only decorated span is an answer slot, not ignorable
        # layout whitespace. Keep it separate from text and adjacent slots.
        raw_text=''.join(element.itertext()) if declares_underline else ''
        if (raw_text and not raw_text.strip()
                and not any(_tag_name(e) in {'img','br','table','input','hr'} for e in element.iter())):
            nodes.append({'type':'blank','kind':'underline','source_text':raw_text})
            return
        parts = _punctuation_slot_parts(element, raw_text) if declares_underline else None
        if parts is not None:
            for index, part in enumerate(parts):
                if index % 2:
                    nodes.append({'type':'blank','kind':'underline','source_text':part})
                else:
                    add_text(part, underlined)
            return
        if tag == "img":
            image_index += 1
            url = normalize_display_text(element.get("src", ""))
            slot = f"{role}.image.{image_index}"
            nodes.append(
                {
                    "type": "image",
                    "id": stable_id("imgref", owner_id, slot, url),
                    "slot": slot,
                    "remote_url": url,
                    "asset_id": None,
                    "declared_width": _parse_dimension(element.get("width")),
                    "declared_height": _parse_dimension(element.get("height")),
                    "alt": normalize_display_text(element.get("alt", "")),
                    "display_mode": "block" if role.startswith("material") else "inline",
                }
            )
            return
        if tag == "br":
            add_break()
            return

        add_text(element.text, underlined)
        children = list(element)
        index = 0
        while index < len(children):
            run = _fragmented_underline_run(children, index)
            if run is not None:
                index, raw = run
                nodes.append({'type':'blank','kind':'underline','source_text':raw})
                add_text(children[index - 1].tail, underlined)
                continue
            child = children[index]
            walk(child, underlined)
            add_text(child.tail, underlined)
            index += 1

    for element in elements:
        walk(element)
        if _tag_name(element) in BLOCK_TAGS:
            add_break()

    while nodes and nodes[-1]["type"] == "break":
        nodes.pop()

    return content_from_nodes(nodes, owner_id=owner_id, role=role)


def content_from_nodes(
    source_nodes: Iterable[dict[str, Any]],
    *,
    owner_id: str,
    role: str,
) -> dict[str, Any]:
    """Finalize ordered rich nodes after a parser splits one HTML block.

    Re-keying image slots here is important when old papers place the stem and
    all options in a single paragraph.
    """
    nodes = copy.deepcopy(list(source_nodes))
    for node in nodes:
        if node.get('type') == 'text': validate_marks(node)
        elif node.get('type') == 'blank': validate_blank(node)
    image_index = 0
    for node in nodes:
        if node.get("type") != "image":
            continue
        image_index += 1
        slot = f"{role}.image.{image_index}"
        node["slot"] = slot
        node["id"] = stable_id("imgref", owner_id, slot, node.get("remote_url", ""))
        node["display_mode"] = "block" if role.startswith("material") else "inline"

    while nodes and nodes[-1].get("type") == "break":
        nodes.pop()

    plain_parts: list[str] = []
    for node in nodes:
        if node["type"] == "text":
            plain_parts.append(node["text"])
        elif node["type"] == "image":
            plain_parts.append("[图片]")
        elif node['type'] == 'blank':
            plain_parts.append('[答案填写位]' if node.get('role')=='answer_placeholder' else '[填空]')
        elif node["type"] == "break":
            plain_parts.append("\n")
    plain_text = " ".join(plain_parts).replace(" \n ", "\n").strip()

    result = {
        "schema_version": (4 if any('source_fragments' in n for n in nodes) else
                           2 if any(n.get('type')=='blank' and 'role' in n for n in nodes) else 1),
        "nodes": nodes,
        "plain_text": plain_text,
        "normalized_text": normalize_match_text(plain_text),
        "image_count": image_index,
    }
    content_blanks(result)
    return result


def content_image_nodes(content: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not content:
        return []
    return [node for node in content.get("nodes", []) if node.get("type") == "image"]


def validate_marks(node):
    """Offsets count Unicode code points in this node's normalized display text."""
    end=0; text=node.get('text','')
    for mark in node.get('marks',[]):
        if (set(mark)!={'kind','start','end','quote'} or mark['kind']!='underline'
                or type(mark['start']) is not int or type(mark['end']) is not int
                or not end<=mark['start']<mark['end']<=len(text)
                or text[mark['start']:mark['end']]!=mark['quote']):
            raise ValueError('画线标记范围、顺序或引文不符')
        end=mark['end']


def text_slice(node, start=0, end=None):
    """Clip and rebase annotations when option extraction removes/splits text."""
    validate_marks(node); end=len(node['text']) if end is None else end
    result={'type':'text','text':node['text'][start:end]}; marks=[]
    for m in node.get('marks',[]):
        a,b=max(start,m['start']),min(end,m['end'])
        if a<b:marks.append({'kind':m['kind'],'start':a-start,'end':b-start,'quote':node['text'][a:b]})
    if marks:result['marks']=marks
    return result


def content_marks(content):
    from .referent_content import positions
    positions(content)
    from .source_paragraphs import paragraphs
    paragraphs(content)
    if content.get('schema_version')==4 or any('source_fragments' in n for n in content.get('nodes',[])):
        content_blanks(content)
    result=[]
    for index,node in enumerate(content.get('nodes',[])):
        if node.get('type')=='text':
            validate_marks(node)
            result.extend({'node_index':index,**m} for m in node.get('marks',[]))
    return result


def validate_blank(node):
    fields={'type','kind','source_text'}
    if 'source_fragments' in node:
        fields.add('source_fragments')
        parts=node['source_fragments']
        if (node.get('kind')!='underline' or 'role' in node or not isinstance(parts,list)
                or not 2<=len(parts)<=32
                or any(not isinstance(p,str) or not 1<=len(p)<=4096 or not p.isspace()
                       or any(c in p for c in '\r\n\t') for p in parts)
                or ''.join(parts)!=node.get('source_text') or len(''.join(parts))>10000):
            raise ValueError('复核填空线必须保留有序、非空且逐字一致的原始片段')
    if 'role' in node:
        fields.add('role')
        if node.get('kind')!='underline' or node['role'] not in {'fill','answer_placeholder'}:
            raise ValueError('下划线角色只能为正文填空或答案填写位')
    if (set(node)!=fields or node.get('type')!='blank'
            or not isinstance(node.get('source_text'),str)):
        raise ValueError('填空节点字段或来源文本无效')
    text=node['source_text']
    if node.get('kind')=='parentheses':
        if not re.fullmatch(r'\([^\S\r\n\t]*\)|（[^\S\r\n\t]*）',text):
            raise ValueError('括号空位必须保留完整配对的空括号，不含答案或换行')
    elif node.get('kind')=='literal_underscore':
        if not re.fullmatch(r'_{2,64}|＿{2,64}',text):
            raise ValueError('字面填空线必须保留 2 至 64 个同类原始下划线字符')
    elif node.get('kind')!='underline' or not text or not text.isspace():
        raise ValueError('填空线节点必须保留非空的原始画线空白')


def content_blanks(content):
    from .referent_content import positions
    positions(content)
    from .source_paragraphs import paragraphs
    paragraphs(content)
    result=[]
    for index,node in enumerate(content.get('nodes',[])):
        if node.get('type')=='blank':
            validate_blank(node)
            value={'node_index':index,'kind':node['kind'],'source_text':node['source_text']}
            if 'role' in node:value['role']=node['role']
            if 'source_fragments' in node:value['source_fragments']=copy.deepcopy(node['source_fragments'])
            result.append(value)
    reviewed=content.get('schema_version')==2
    if (any('role' in b for b in result) and not reviewed
            or reviewed and (not result or any('role' not in b for b in result))):
        raise ValueError('富文本v2须完整标注所有下划线角色，旧版本不能携带角色')
    grouped=any('source_fragments' in n for n in content.get('nodes',[]))
    if grouped or content.get('schema_version')==4:
        nodes=content.get('nodes',[])
        if (type(content.get('schema_version')) is not int or content['schema_version']!=4 or not grouped
                or set(content)!={'schema_version','nodes','plain_text','normalized_text','image_count'}
                or content['image_count']!=0 or not 1<=len(nodes)<=2000
                or any(n.get('type') not in {'text','blank'} or 'role' in n
                       or (n['type']=='text' and (set(n)!={'type','text'} or not isinstance(n['text'],str)))
                       or (n['type']=='blank' and n.get('kind')!='underline') for n in nodes)):
            raise ValueError('富文本v4只接受经复核的纯文本填空线分组')
        plain=' '.join(n['text'] if n['type']=='text' else '[填空]' for n in nodes).strip()
        if content['plain_text']!=plain or content['normalized_text']!=normalize_match_text(plain):
            raise ValueError('填空线分组的文本投影不一致')
    return result


def has_annotations(content):
    from .referent_content import positions
    marks=content_marks(content);blanks=content_blanks(content)
    return bool(marks or blanks or positions(content))


def blank_display(node):
    """A bounded visible line, not an inferred answer length or original pixels."""
    validate_blank(node)
    if node['kind'] in {'parentheses','literal_underscore'}:return node['source_text']
    width=sum(4 if c=='\t' else 2 if c=='\u3000' else 1 for c in node['source_text'])
    return '_'*max(4,min(40,width))


def without_marks(content):
    result=copy.deepcopy(content)
    for node in result.get('nodes',[]):node.pop('marks',None)
    return result


def text_runs(node):
    validate_marks(node); cursor=0
    for m in node.get('marks',[]):
        if cursor<m['start']:yield node['text'][cursor:m['start']],False
        yield m['quote'],True;cursor=m['end']
    if cursor<len(node.get('text','')):yield node['text'][cursor:],False


def replace_asset_ids(content: dict[str, Any], url_map: dict[str, str]) -> None:
    for node in content.get("nodes", []):
        if node.get("type") == "image":
            node["asset_id"] = url_map.get(node.get("remote_url", ""))
