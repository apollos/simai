"""CJK-aware text segmentation for FTS5.

SQLite's unicode61 tokenizer treats a run of Chinese characters as one
token, which breaks substring keyword search.  We insert spaces between
CJK characters before indexing, and turn CJK runs in queries back into
FTS5 phrases so multi-character terms match exactly.

A whole natural-language question is also one CJK run, and requiring it
to appear verbatim would match nothing.  Runs longer than a plausible
term are therefore expanded into overlapping bigrams combined with OR,
leaving BM25 to rank documents that match more of them.
"""

from __future__ import annotations

import re

_CJK_CHAR = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af])")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")
_FTS_SPECIAL = re.compile(r'["*^()]|\bAND\b|\bOR\b|\bNOT\b')
_WORD = re.compile(r"[0-9A-Za-z_]+")

# Runs up to this length are treated as a single term ("知识管理"); longer
# runs are sentences and get the bigram treatment.
_PHRASE_MAX_CHARS = 4
# Bounds the MATCH expression for long inputs such as placement lookups.
_MAX_TERMS = 64


def segment_for_index(text: str) -> str:
    """'小团队自治 works' -> '小 团 队 自 治 works'"""
    return _CJK_CHAR.sub(r" \1 ", text or "")


def build_match_query(query: str) -> str:
    """Turn a user query into a safe FTS5 MATCH expression."""
    query = _FTS_SPECIAL.sub(" ", query or "")
    terms: list[str] = []
    pos = 0
    for match in _CJK_RUN.finditer(query):
        terms.extend(_plain_terms(query[pos : match.start()]))
        terms.extend(_cjk_terms(match.group(0)))
        pos = match.end()
    terms.extend(_plain_terms(query[pos:]))
    return " OR ".join(_dedupe(terms)[:_MAX_TERMS])


def _cjk_terms(run: str) -> list[str]:
    if len(run) <= _PHRASE_MAX_CHARS:
        return [f'"{" ".join(run)}"']
    return [f'"{run[i]} {run[i + 1]}"' for i in range(len(run) - 1)]


def _plain_terms(fragment: str) -> list[str]:
    """Alphanumeric words only; punctuation must never reach the MATCH
    expression, where it would become an unsatisfiable bare term."""
    return _WORD.findall(fragment)


def _dedupe(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    return [t for t in terms if not (t in seen or seen.add(t))]
