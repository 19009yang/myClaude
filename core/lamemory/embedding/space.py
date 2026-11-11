"""不含凭据的向量空间描述与确定性输入模板。"""
from dataclasses import dataclass, asdict
import hashlib
import json


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class EmbeddingDescriptor:
    model: str
    dimensions: int
    endpoint_fingerprint: str
    namespace: str
    query_prefix: str = ''
    document_prefix: str = ''
    max_embedding_chars: int = 12000
    template_version: str = 'lamemory-v1'
    normalization: str = 'l2-float32-le-v1'

    def __post_init__(self):
        if not self.model or not self.namespace or type(self.dimensions) is not int or self.dimensions <= 0:
            raise ValueError('embedding 空间必须指定 model/dimensions/namespace')
        if self.max_embedding_chars <= 0:
            raise ValueError('无效输入上限')

    @property
    def json(self):
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)

    @property
    def space_id(self):
        return digest(self.json)

    def document_input(self, text):
        return (self.document_prefix + text)[:self.max_embedding_chars]
