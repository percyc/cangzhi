"""Bounded, model-free source-anchor selection after document recall.

Only compares rows already authorized by the two retrievers. Never changes
document ranking, source content, coordinates, or the amount of output context.
"""
from __future__ import annotations

import math
import re
import unicodedata

MAX_ROWS_PER_CHANNEL = 8
MAX_QUERY_CHARS = 512
MAX_BODY_CHARS = 16_000
MAX_FEATURES = 128


def _normalize(value):
    return "".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def _word_text(value):
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"\b([a-z]+)\s+(?=\d)", r"\1", value)


def _matches(term, compact, words):
    if re.search(r"[\u3400-\u9fff]", term):
        return term in compact
    # Do not match "rate" inside "corporate" or join English words across
    # whitespace. Chinese PDF spacing and spaced identifiers remain supported.
    return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", words) is not None


def _features(query):
    # Uniformly sample over the whole question if the budget is exceeded;
    # retaining only a prefix loses predicates after long document names.
    normalized = unicodedata.normalize("NFKC", query[:MAX_QUERY_CHARS]).casefold()
    # Keep a spaced identifier (ISO 9001 / GB 1234) together. A bare shared
    # prefix must not reward a references list containing unrelated standards.
    normalized = re.sub(r"\b([a-z]+)\s+(?=\d)", r"\1", normalized)
    features = []
    for run in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._/-][a-z0-9]+)*", normalized):
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            for start in range(len(run)):
                for size in (2, 3):
                    if start + size <= len(run):
                        features.append(run[start:start + size])
        else:
            features.append(run)
    features = list(dict.fromkeys(features))
    if len(features) > MAX_FEATURES:
        features = [features[i * (len(features)-1) // (MAX_FEATURES-1)]
                    for i in range(MAX_FEATURES)]
    return features


def choose_evidence_row(query, title, lexical_rows, vector_rows):
    """Choose within one document/version; ties retain the serving anchor.

    Query-feature coverage (not occurrence count) avoids rewarding repeated
    boilerplate. Features occurring in the document title are downweighted;
    candidate-local IDF emphasizes discriminating body facts. Original channel
    order breaks ties, never incomparable FTS/cosine raw scores.
    """
    lexical = list(lexical_rows[:MAX_ROWS_PER_CHANNEL])
    vector = list(vector_rows[:MAX_ROWS_PER_CHANNEL])
    if not lexical and not vector:
        return None
    anchor = (lexical or vector)[0]
    rows = {}
    ranks = {}
    for channel in (lexical, vector):
        seen = set()
        for index, row in enumerate(channel):
            if (row.document_id != anchor.document_id
                    or row.document_version_id != anchor.document_version_id
                    or row.chunk_id in seen):
                continue
            seen.add(row.chunk_id)
            rows.setdefault(row.chunk_id, row)
            ranks[row.chunk_id] = ranks.get(row.chunk_id, 0) + 1 / (60 + index + 1)
    features = _features(query)
    if len(rows) < 2 or not features:
        return anchor
    title_text = _normalize(title)
    title_words = _word_text(title)
    bodies = {key: _normalize(row.content[:MAX_BODY_CHARS]) for key, row in rows.items()}
    words = {key: _word_text(row.content[:MAX_BODY_CHARS]) for key, row in rows.items()}
    present = {term: {key for key, body in bodies.items() if _matches(term, body, words[key])} for term in features}
    in_title = {term: _matches(term, title_text, title_words) for term in features}
    # Title-only queries have no evidence-selection signal; preserve recall.
    focus = [term for term in features if not in_title[term] and present[term]]
    if not focus:
        return anchor
    weights = {term: (0.15 if in_title[term] else 1.0)
               * (1 + math.log((1 + len(rows)) / (1 + len(present[term]))))
               for term in features}
    scores = {key: sum(weights[term] for term in features if key in present[term])
              for key in rows}
    winner = max(rows, key=lambda key: (scores[key], key == anchor.chunk_id, ranks[key], -key))
    return rows[winner]
