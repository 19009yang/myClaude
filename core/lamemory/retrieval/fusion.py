"""无 IO 的向量验证、cosine 与按身份加权 RRF。"""
import math
from numbers import Real


def unit_vector(vector, dimensions=None):
    if not isinstance(vector, (list, tuple)) or not vector or (dimensions is not None and len(vector) != dimensions):
        raise ValueError('invalid_vector_dimensions')
    if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in vector):
        raise ValueError('invalid_vector_values')
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise ValueError('invalid_vector_norm')
    return [v / norm for v in vector]


def cosine(a, b):
    a = unit_vector(a)
    b = unit_vector(b, len(a))
    return max(-1.0, min(1.0, math.fsum(x*y for x, y in zip(a, b))))


def rrf(channels, *, k=60, weights=None, revisions=None):
    if type(k) is not int or k <= 0:
        raise ValueError('rrf_k 必须为正整数')
    if set(channels) - {'keyword', 'semantic'}:
        raise ValueError('unknown_channel')
    weights = weights or {}
    if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) or v <= 0 for v in weights.values()):
        raise ValueError('invalid_rrf_weight')
    revisions = revisions or {}
    fused = {}
    for channel, hits in channels.items():
        seen = set()
        for identity, raw_score in hits:
            if identity in seen:
                continue
            seen.add(identity)
            rank = len(seen)
            result = fused.setdefault(identity, {'rrf_score': 0.0, 'keyword_rank': None, 'keyword_score': None,
                                                'semantic_rank': None, 'semantic_score': None, 'rerank_score': None})
            result[channel + '_rank'] = rank
            result[channel + '_score'] = raw_score
            result['rrf_score'] += weights.get(channel, 1.0) / (k + rank)
    return sorted(fused.items(), key=lambda pair: (-pair[1]['rrf_score'],
                  min(v for n, v in pair[1].items() if n.endswith('_rank') and v is not None),
                  -revisions.get(pair[0], 0), pair[0]))
