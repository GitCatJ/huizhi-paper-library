"""Bounded paragraph display derived from verified archived HTML evidence.

Rich text v3 retains paragraph boundaries, exact source text/whitespace,
explicit alignment and whole-paragraph bold. Source authorization belongs to
the revision ledger, not to this display value. Unsupported formatting fails
closed; arbitrary source CSS or HTML is never forwarded to a renderer.
"""
import copy
import re

from .utils import normalize_display_text, normalize_match_text

CONTRACT = 'source_paragraph_display_v1'
ALIGNMENTS = {None, 'left', 'center', 'right', 'justify'}
# Audited local export template: font/size/spacing and Bootstrap row columns.
# It does not override paragraph alignment, indentation or bold declarations.
SUPPORTED_STYLESHEETS = {'f38abbfd4ec996f9396b85c927c9c06418e2a8aa6039ee94ddf87a2e2bdad249'}
MAX_PARAGRAPHS = 500
MAX_CHARACTERS = 100_000


def _check_text(text):
    if (not isinstance(text, str) or len(text) > MAX_CHARACTERS
            or re.search(r'[\x00-\x08\x0a-\x1f\ud800-\udfff\ufffe\uffff]', text)):
        raise ValueError('段落文字包含未支持的控制字符、换行或长度')


def _compose(paragraphs):
    if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= MAX_PARAGRAPHS:
        raise ValueError('段落数量超出支持范围')
    nodes, layout, text = [], [], []
    for ordinal, paragraph in enumerate(paragraphs, 1):
        if not isinstance(paragraph, dict) or set(paragraph) != {'text', 'alignment', 'bold'}:
            raise ValueError('段落显示字段不符')
        _check_text(paragraph['text'])
        if ((paragraph['alignment'] is not None and not isinstance(paragraph['alignment'], str))
                or paragraph['alignment'] not in ALIGNMENTS or type(paragraph['bold']) is not bool):
            raise ValueError('段落对齐或加粗声明不符')
        if nodes:
            nodes.append({'type': 'break'})
        layout.append({'ordinal': ordinal, 'node_index': len(nodes),
                       'alignment': paragraph['alignment'], 'bold': paragraph['bold']})
        nodes.append({'type': 'text', 'text': paragraph['text']})
        text.append(paragraph['text'])
    plain = '\n'.join(text)
    if len(plain) > MAX_CHARACTERS:
        raise ValueError('段落文字总量超出支持范围')
    return {'schema_version': 3, 'nodes': nodes, 'plain_text': plain,
            'normalized_text': normalize_match_text(plain), 'image_count': 0,
            'paragraph_layout': {'contract': CONTRACT, 'paragraphs': layout}}


def paragraphs(content):
    """Validate every field; legacy content returns None without alteration."""
    if not content:
        return None
    if content.get('schema_version') != 3 and 'paragraph_layout' not in content:
        return None
    fields = {'schema_version', 'nodes', 'plain_text', 'normalized_text', 'image_count', 'paragraph_layout'}
    layout = content.get('paragraph_layout')
    if (set(content) != fields or type(content.get('schema_version')) is not int
            or content['schema_version'] != 3 or type(content['image_count']) is not int
            or not isinstance(layout, dict) or set(layout) != {'contract', 'paragraphs'}
            or layout['contract'] != CONTRACT or not isinstance(layout['paragraphs'], list)
            or not 1 <= len(layout['paragraphs']) <= MAX_PARAGRAPHS
            or not isinstance(content['nodes'], list)
            or len(content['nodes']) != 2 * len(layout['paragraphs']) - 1):
        raise ValueError('段落富文本版本、字段或数量不符')
    result = []
    for ordinal, item in enumerate(layout['paragraphs'], 1):
        index = (ordinal - 1) * 2
        if (not isinstance(item, dict) or set(item) != {'ordinal', 'node_index', 'alignment', 'bold'}
                or type(item['ordinal']) is not int or item['ordinal'] != ordinal
                or type(item['node_index']) is not int or item['node_index'] != index):
            raise ValueError('段落位置与节点顺序不符')
        node = content['nodes'][index]
        if (not isinstance(node, dict) or set(node) != {'type', 'text'} or node['type'] != 'text'
                or (index and content['nodes'][index - 1] != {'type': 'break'})):
            raise ValueError('段落节点包含未支持的内容或分隔')
        result.append({'text': node['text'], 'alignment': item['alignment'], 'bold': item['bold']})
    if content != _compose(result):
        raise ValueError('段落显示投影与原始节点不一致')
    return result


def identity(content):
    """Include whitespace/layout in strict identity, without source-local IDs."""
    value = paragraphs(content)
    return None if value is None else {'contract': CONTRACT, 'paragraphs': copy.deepcopy(value)}
