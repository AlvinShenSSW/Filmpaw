"""Name normalization per design §4.

normalize(s) = zhconv_to_simplified(lower(NFKC(s.strip())))
- NFKC folds full-width forms (incl. full-width parentheses) to half-width
- zhconv folds traditional → simplified (倉木華 → 仓木华)
- lower() only affects Latin letters
"""

import unicodedata

from zhconv import convert


def normalize(s: str) -> str:
    return convert(unicodedata.normalize("NFKC", s.strip()).lower(), "zh-hans")


def bidirectional_match(record_norm: str, query_norm: str) -> bool:
    """Design §4 matching rule: record contains query, OR query contains
    record (record must be >= 2 chars to match in reverse, guarding against
    single-character noise)."""
    if not query_norm:
        return False
    if query_norm in record_norm:
        return True
    return len(record_norm) >= 2 and record_norm in query_norm
