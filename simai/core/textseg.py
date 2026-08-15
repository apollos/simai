"""CJK-aware text segmentation for FTS5.

SQLite's unicode61 tokenizer treats a run of Chinese characters as one
token, which breaks substring keyword search.  We insert spaces between
CJK characters before indexing, and convert CJK runs in queries into
FTS5 phrase queries ("自 治") so multi-character terms match exactly.
"""

from __future__ import annotations

import re

_CJK_CHAR = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af])")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")
_FTS_SPECIAL = re.compile(r'["*^()]|\bAND\b|\bOR\b|\bNOT\b')


def segment_for_index(text: str) -> str:
    """'小团队自治 works' -> '小 团 队 自 治 works'"""
    return _CJK_CHAR.sub(r" \1 ", text or "")


def build_match_query(query: str) -> str:
    """Turn a user query into a safe FTS5 MATCH expression.
    CJK runs become phrases; other words are kept as prefix terms."""
    query = _FTS_SPECIAL.sub(" ", query or "")
    parts: list[str] = []
    pos = 0
    for m in _CJK_RUN.finditer(query):
        parts.extend(_plain_terms(query[pos : m.start()]))
        chars = " ".join(m.group(0))
        parts.append(f'"{chars}"')
        pos = m.end()
    parts.extend(_plain_terms(query[pos:]))
    return " ".join(parts)


def _plain_terms(fragment: str) -> list[str]:
    return [w for w in re.split(r"\s+", fragment) if w]
