"""Rich content v5: source glyphs with explicit reference/position identity.

Text nodes contain the original circled glyph for every renderer. The search
projection remains NFKC, while fingerprints include the positional contract.
No source authority is implied by a structurally valid content object.
"""
import copy
import unicodedata

from .utils import normalize_match_text

VERSION = 5
GLYPHS = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
FIELDS = {'node_index','start','end','glyph','usage'}


def present(content):
    return content.get('schema_version') == VERSION or 'source_referents' in content


def _plain(nodes):
    return ' '.join(n['text'] if n['type']=='text' else '[图片]' if n['type']=='image' else '\n'
                    for n in nodes).replace(' \n ','\n').strip()


def positions(content):
    if not present(content):return []
    from .richtext import validate_marks
    nodes=content.get('nodes'); refs=content.get('source_referents')
    if (type(content.get('schema_version')) is not int or content['schema_version'] != VERSION
            or set(content) != {'schema_version','nodes','plain_text','normalized_text','image_count','source_referents'}
            or not isinstance(nodes,list) or not 1<=len(nodes)<=2000
            or not isinstance(refs,list) or not 1<=len(refs)<=20
            or any(not isinstance(n,dict) or n.get('type') not in {'text','break','image'} for n in nodes)):
        raise ValueError('圈号富文本v5字段、节点或指代数量无效')
    for n in nodes:
        if n['type']=='text':
            if set(n)-{'type','text','marks'} or not isinstance(n.get('text'),str):
                raise ValueError('圈号文字节点无效')
            validate_marks(n)
    if (type(content['image_count']) is not int
            or content['image_count']!=sum(n['type']=='image' for n in nodes)
            or content['plain_text']!=_plain(nodes)
            or content['normalized_text']!=normalize_match_text(content['plain_text'])):
        raise ValueError('圈号内容的文字或图片投影不一致')
    seen=set();previous=(-1,-1)
    for ref in refs:
        if (not isinstance(ref,dict) or set(ref)!=FIELDS
                or any(type(ref.get(k)) is not int for k in ('node_index','start','end'))
                or not 0<=ref['node_index']<len(nodes) or not isinstance(ref['glyph'],str) or ref['glyph'] not in GLYPHS
                or len(ref['glyph'])!=1 or ref['usage'] not in {'reference','position'}):
            raise ValueError('圈号身份或定位字段无效')
        n=nodes[ref['node_index']]; key=(ref['node_index'],ref['start'])
        if (n['type']!='text' or not 0<=ref['start']<ref['end']<=len(n['text'])
                or ref['end']!=ref['start']+1 or n['text'][ref['start']:ref['end']]!=ref['glyph']
                or key<=previous or ref['glyph'] in seen):
            raise ValueError('圈号范围、顺序、唯一性或原文字符不符')
        seen.add(ref['glyph']);previous=key
    return copy.deepcopy(refs)


def restore(content, spans):
    """Replace only source-proven NFKC digits, preserving existing underline spans.

Spans address the unmodified parser result. DOM re-derivation belongs to the
caller; this transformation is deliberately unusable as publication authority.
"""
    from .richtext import validate_marks
    if content.get('schema_version')!=1 or present(content):raise ValueError('圈号恢复只接受原始v1内容')
    if not spans:return copy.deepcopy(content)
    result=copy.deepcopy(content); refs=[]
    for index,node in enumerate(result['nodes']):
        selected=sorted((s for s in spans if s['node_index']==index),key=lambda s:s['start'])
        if not selected:continue
        if node['type']!='text':raise ValueError('圈号必须位于文字节点')
        validate_marks(node); original=node['text']; cursor=0; parts=[];delta=0; mapping=[]
        for span in selected:
            start,end=span['start'],span['end'];glyph=span['glyph']
            if (set(span)!=FIELDS or glyph not in GLYPHS or len(glyph)!=1
                    or not cursor<=start<end<=len(original)
                    or original[start:end]!=unicodedata.normalize('NFKC',glyph)):
                raise ValueError('圈号的原始数字、范围或来源不符')
            parts.extend((original[cursor:start],glyph));a=start+delta
            refs.append({**span,'start':a,'end':a+1});mapping.append((start,end,1-(end-start)))
            delta+=1-(end-start);cursor=end
        parts.append(original[cursor:]);node['text']=''.join(parts)
        def offset(value):
            if any(a<value<b for a,b,_ in mapping):raise ValueError('下划线边界截断了来源圈号')
            return value+sum(d for _,b,d in mapping if b<=value)
        for mark in node.get('marks',[]):
            mark['start']=offset(mark['start']);mark['end']=offset(mark['end'])
            mark['quote']=node['text'][mark['start']:mark['end']]
    if len(refs)!=len(spans):raise ValueError('圈号指向不存在的节点')
    result.update(schema_version=VERSION,source_referents=refs,plain_text=_plain(result['nodes']))
    result['normalized_text']=normalize_match_text(result['plain_text']);positions(result)
    return result


def restoration(old,new):
    """Conservative shape check; source verification must still bind exact bytes."""
    from .richtext import content_image_nodes,validate_marks
    refs=positions(new)
    if not refs or old.get('schema_version')!=1 or old.get('normalized_text')!=new['normalized_text']:
        return False
    if len(old.get('nodes',[]))!=len(new['nodes']):return False
    if content_image_nodes(old)!=content_image_nodes(new):return False
    for a,b in zip(old['nodes'],new['nodes']):
        if a['type']!=b['type']:return False
        if a['type']=='text':
            validate_marks(a);validate_marks(b)
            if unicodedata.normalize('NFKC',a['text'])!=unicodedata.normalize('NFKC',b['text']):return False
            # Adding observed marks is allowed. Existing marks cannot disappear.
            if a.get('marks'):
                def marks(n):
                    return [(len(unicodedata.normalize('NFKC',n['text'][:m['start']])),
                             len(unicodedata.normalize('NFKC',n['text'][:m['end']])),
                             unicodedata.normalize('NFKC',m['quote'])) for m in n.get('marks',[])]
                if marks(a)!=marks(b):return False
        elif a!=b:return False
    return True


def reference_matches(stem,materials):
    refs=positions(stem)
    if not refs:return None
    if any(r['usage']!='reference' for r in refs):return False
    targets=[r for m in materials for r in positions(m) if r['usage']=='position']
    return all(sum(t['glyph']==r['glyph'] for t in targets)==1 for r in refs)
