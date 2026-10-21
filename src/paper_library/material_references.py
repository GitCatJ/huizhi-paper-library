"""Resolve bounded source labels, never incidental scores or list positions."""
import re

NUM = r'(?:[0-9]+|[一二三四五六七八九十百零〇两]+)'
REF = re.compile(rf'(?:给定\s*)?(?:材料|资料)\s*[（(]?\s*({NUM}(?:\s*(?:、|和|及|与|[-~～—至到])\s*{NUM})*)')
LABEL = re.compile(rf'^(?:给定\s*)?(?:材料|资料)\s*({NUM})\s*$')
RANGE = re.compile(rf'^({NUM})\s*[-~～—至到]\s*({NUM})$')


def number(token):
    if token.isdecimal(): return int(token)
    digits=dict(zip('零〇一二两三四五六七八九','001223456789'))
    total=0; current=0
    for char in token:
        if char in digits: current=int(digits[char])
        elif char in '十百': total+=(current or 1)*({'十':10,'百':100}[char]); current=0
        else: return None
    return total+current


def numbered_scope(text, materials):
    matches=list(REF.finditer(text))
    if not matches: return None
    labels={}; aliases={}; tokens=[]
    for material in materials:
        match=LABEL.fullmatch(material.get('label',''))
        token=match[1] if match else None; tokens.append(token)
        if token:
            labels.setdefault(token,[]).append(material['id'])
            aliases.setdefault(number(token),[]).append(material['id'])
    evidence='；'.join(match[0] for match in matches)
    resolved=[]; failed=False
    for match in matches:
        for part in re.split(r'\s*(?:、|和|及|与)\s*',match[1]):
            span=RANGE.fullmatch(part)
            if span:
                start,end=number(span[1]),number(span[2])
                if start is None or end is None or not 1<=start<=end<=1000:
                    failed=True;continue
                # Preserve numeral style in ranges when both styles coexist.
                for value in range(start,end+1):
                    pool=[m for token,ids in labels.items() if number(token)==value and token.isdecimal()==span[1].isdecimal() for m in ids]
                    pool=pool or aliases.get(value,[])
                    if len(pool)!=1: failed=True
                    else: resolved.append(pool[0])
            else:
                pool=labels.get(part) or aliases.get(number(part),[])
                if len(pool)!=1: failed=True
                else: resolved.append(pool[0])
    selected=set(resolved)
    # Mixed numbering can denote nested material groups. A parent-only reference
    # cannot safely discard its following Chinese-numbered components.
    for index,token in enumerate(tokens[:-1]):
        following=tokens[index+1]
        if token and token.isdecimal() and following and not following.isdecimal() and number(following)==1:
            children=[]
            for j in range(index+1,len(tokens)):
                if not tokens[j] or tokens[j].isdecimal(): break
                children.append(materials[j]['id'])
            if materials[index]['id'] in selected and not selected.intersection(children): failed=True
    return {'scope_type':'unresolved' if failed else 'explicit_list','raw_expression':evidence,
            'material_ids':[] if failed else list(dict.fromkeys(resolved)), 'confidence':0.0 if failed else 0.95}


def references_material(text):
    return bool(REF.search(text) or re.search(
        r'(?:给\s*定|所给|全部|所有|上述|以上|下列|以下)\s*(?:材\s*料|资\s*料)'
        r'|(?:阅读|根据|依据|结合|运用|分析|参考|紧扣)\s*[“"\']?\s*(?:材料|资料)'
        r'|(?:结合|根据|依据|参照|参考)\s*(?:上述|以上|前述|下述|下列)?\s*(?:案例|笔记)'
        r'|本文|上文|下文|材料中|资料中'
        r'|(?:根据|依据|参照|参考)\s*(?:图|表)\s*[一二三四五六七八九十0-9]+', text))
