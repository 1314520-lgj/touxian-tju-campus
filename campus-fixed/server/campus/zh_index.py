"""A local narrowing index for Chinese notice text.

The remote Meilisearch index is optional. Without it, search compared every
query term against every paragraph of every published event: correct, but
linear in the size of the corpus.

This module keeps a SQLite FTS5 table beside the records so that fallback can
touch far fewer rows. Two choices are worth stating:

* Han text has no word boundaries, so it is tokenised here and stored
  space-separated. Each Han run contributes its unigrams and bigrams. Because
  every substring of two or more characters contains all of its own bigrams, an
  ``AND`` over a term's tokens can never miss a substring match. The index
  therefore only narrows the work, never the result — which is what makes it
  safe to place underneath an existing scorer without changing what is found.
* Word segmentation (jieba and friends) is deliberately not used. It would move
  token boundaries and break that superset property, and an index that silently
  drops matches is worse than a linear scan.

Why this does not rank. FTS5 offers ``bm25()`` with per-column weights, and the
obvious move is to score title above body with it. That was measured and
rejected. The event title is stored on every segment row, so a title match makes
*all* of an event's paragraphs match, and BM25's length normalisation then
prefers the shortest one. On a real notice — title "关于2026年暑期社会实践活动的通知",
body paragraphs ranging from "好的" to a full paragraph of requirements — the
ordering came out as the one-word paragraph first and the paragraph that
actually states the requirements last, ahead of even a paragraph containing none
of the query terms. Ranking therefore stays with the caller, which can tell a
title match from a body match and can keep one scoring function across the
indexed and unindexed paths instead of merging two incomparable scales.

The index is an accelerator, never a source of truth: a caller may use it only
for an event version whose receipt says the rows are current, and must fall
back to a full scan otherwise.
"""

from __future__ import annotations

import re

from sqlalchemy import select

TABLE = "campus_segment_fts"
RECEIPTS = "local_index_receipts"

_HAN = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_PIECE = re.compile(f"[{_HAN}]+|[a-z0-9]+")
_WORD = re.compile(r"[a-z0-9]+")
_PURE_HAN = re.compile(f"^[{_HAN}]+$")


def narrowable(terms):
    """Whether every term is pure Han text.

    Only Han terms carry the bigram guarantee. An ASCII term is matched by the
    existing scanner as a substring, so ``gpa`` finds ``gpa3.5`` — a token index
    would not. Rather than answer such a query with a silently smaller result
    set, the caller scans in full.
    """
    return bool(terms) and all(_PURE_HAN.match(str(term)) for term in terms)


def tokenize(text):
    """Tokens for storage and for querying; one function serves both sides.

    A run of Han characters contributes its unigrams and its bigrams; a run of
    ASCII letters or digits contributes one lowercased token.
    """
    tokens = []
    for match in _PIECE.finditer(str(text or "").lower()):
        piece = match.group(0)
        if _WORD.fullmatch(piece):
            tokens.append(piece)
            continue
        tokens.extend(piece)
        tokens.extend(piece[index : index + 2] for index in range(len(piece) - 1))
    return tokens


def token_text(text):
    return " ".join(tokenize(text))


def match_expression(terms):
    """A safe FTS5 MATCH expression: AND inside a term, OR across terms.

    Terms come from user input, so every token is quoted and embedded quotes are
    doubled; no term text is ever spliced into the query as syntax.
    """
    clauses = []
    for term in terms or ():
        tokens = list(dict.fromkeys(tokenize(term)))
        if not tokens:
            continue
        quoted = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        clauses.append("(" + quoted + ")")
    return " OR ".join(clauses)


class LocalTextIndex:
    """An FTS5 table living in the same SQLite file as the records."""

    def __init__(self, store):
        self.store = store
        self._supported = None

    @property
    def supported(self):
        """Whether this database can host the index. Probed once, then cached."""
        if self._supported is None:
            self._supported = self._probe()
        return self._supported

    def _probe(self):
        if self.store.engine.dialect.name != "sqlite":
            return False
        try:
            with self.store.transaction():
                self._create()
            return True
        except Exception:
            # A build without FTS5, or a read-only file: search stays linear.
            return False

    def _create(self):
        self.store.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5("
            "event_id UNINDEXED, event_version UNINDEXED, snapshot_id UNINDEXED, "
            "segment_id UNINDEXED, title, body, tokenize='unicode61 remove_diacritics 2')"
        )

    def replace(self, event, segments):
        """Make this event's rows exactly match ``segments``. Caller holds a transaction."""
        self.store.execute(f"DELETE FROM {TABLE} WHERE event_id = ?", (event["id"],))
        if event.get("publication_state") != "published":
            return 0
        rows = [
            (
                event["id"],
                event["version"],
                segment["snapshot_id"],
                segment["id"],
                token_text(event["title"]),
                token_text(segment["text"]),
            )
            for segment in segments
        ]
        if rows:
            values = ",".join(["(?,?,?,?,?,?)"] * len(rows))
            self.store.execute(
                f"INSERT INTO {TABLE} (event_id,event_version,snapshot_id,segment_id,title,body) VALUES {values}",
                tuple(value for row in rows for value in row),
            )
        return len(rows)

    def keys(self, terms):
        """``{event_id: {(snapshot_id, segment_id)}}`` for every indexed match.

        Returns ``None`` when the index declines to answer, so a caller can tell
        "no match" apart from "no index". The result is never truncated: a cap
        would silently drop the weakest matches that a full scan would keep.
        """
        return self.lookup(terms)[0]

    def lookup(self, terms):
        """Return matching keys and their receipts from one SQLite snapshot.

        Separate reads can pair old keys with a freshly committed receipt and
        falsely exclude new matches. The caller must use this pair together.
        ``(None, {})`` means the query needs the full substring scan.
        """
        if not self.supported or not narrowable(terms):
            return None, {}
        expression = match_expression(terms)
        if not expression:
            return None, {}
        with self.store.read_connection() as connection:
            rows = connection.exec_driver_sql(
                f"SELECT event_id, snapshot_id, segment_id FROM {TABLE} WHERE {TABLE} MATCH ?", (expression,)
            ).fetchall()
            receipts = (
                connection.execute(select(self.store.records.c.data).where(self.store.records.c.collection == RECEIPTS))
                .scalars()
                .all()
            )
        grouped = {}
        for event_id, snapshot_id, segment_id in rows:
            grouped.setdefault(event_id, set()).add((snapshot_id, segment_id))
        return grouped, {row["event_id"]: row["version"] for row in receipts}

    def receipts(self):
        """``{event_id: version}`` for events whose rows are known to be current."""
        return {row["event_id"]: row["version"] for row in self.store.list(RECEIPTS)}
