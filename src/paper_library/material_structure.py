"""Source material placeholder recognition extracted from 绘智公考."""
import json
import re
from .richtext import content_image_nodes

def is_placeholder(content):
    if isinstance(content, str):
        content = json.loads(content)
    if content_image_nodes(content) or content.get('image_count', 0):
        return False
    text = re.sub(r'[\s，,。.!！；;：:]', '', content.get('plain_text') or content.get('normalized_text', ''))
    return bool(re.fullmatch(r'(?:本部分|本题|本卷)?(?:没有|无)(?:材料|资料)(?:请直接查看试题|请查看试题)?', text))
