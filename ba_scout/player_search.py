"""Conservative name-to-ID lookup using BATrace's public search endpoint.

Search response ratings are deliberately discarded: this endpoint is used only
to suggest identities, never as a current player-statistics source. Call through
the application's shared ``RequestGate.run`` so a 429 also stops other lookups.
HTTP errors, including 429/403, propagate unchanged; there are no retries.

``timeout`` is urllib's operation timeout, not the whole application's deadline.
The caller must still enforce the shared preparation-stage time budget.
"""

from __future__ import annotations

import json
import math
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SEARCH_URL = "https://dash.batrace.top/api/players/search"
LEGACY_SEARCH_LIMIT = 20
# Public endpoint checks on 2026-10-26: limit=1 returned one Lazer row,
# limit=50 returned all 20 observed Lazer rows, and a broad query returned 50.
# The response has no total/hasMore field; a full requested page stays unknown.
SEARCH_LIMIT = 50
MAX_RESPONSE_BYTES = 256 * 1024
MAX_NAME_LENGTH = 256
USER_AGENT = "BA-Scout/0.7.4 (local public player identity lookup)"


class PlayerSearchError(ValueError):
    """The public search response is malformed or cannot be safely interpreted."""


def _limit(value):
    if type(value) is not int or not 1 <= value <= SEARCH_LIMIT:
        raise PlayerSearchError("requested candidate limit must be within 1..50")
    return value


class SearchResults(list):
    """List-compatible candidates with per-request completeness evidence.

    JSON serializes this as a plain list; callers saving diagnostics should also
    save metadata(). ``complete`` means the validated response was smaller than
    the requested supported limit, not that the server supplied a total count.
    """

    def __init__(self, players, *, requested_limit=LEGACY_SEARCH_LIMIT, query=None):
        super().__init__(players)
        self.requested_limit = _limit(requested_limit)
        self.query = normalize_name(query) if query is not None else None
        self.returned_count = len(self)
        self.complete = self.returned_count < self.requested_limit

    def metadata(self):
        return {"query": self.query, "requested_limit": self.requested_limit,
                "returned_count": self.returned_count, "complete": self.complete,
                "completeness_basis": "below_requested_limit" if self.complete else "at_requested_limit"}


def normalize_name(name: str) -> str:
    """Apply NFKC and whitespace normalization, preserving case and punctuation.

    This does not transliterate, remove accents, map visual confusables, or
    substitute OCR lookalikes such as O/0. Empty/invalid names are rejected.
    """
    if not isinstance(name, str):
        raise ValueError("player name must be a string")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError("player name exceeds the length limit")
    normalized = " ".join(unicodedata.normalize("NFKC", name).split())
    if not normalized or len(normalized) > MAX_NAME_LENGTH:
        raise ValueError("player name is empty or exceeds the length limit")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in normalized):
        raise ValueError("player name contains invalid control/surrogate characters")
    return normalized


def _candidate(value: object) -> dict:
    if not isinstance(value, dict):
        raise PlayerSearchError("search player must be an object")
    player_id = value.get("id")
    if isinstance(player_id, bool) or not isinstance(player_id, int) or player_id <= 0:
        raise PlayerSearchError("search player ID must be a positive integer")
    name = value.get("name")
    try:
        normalize_name(name)
    except ValueError as exc:
        raise PlayerSearchError("search player name is invalid") from exc
    # Preserve the public display name; normalization is only for comparison.
    return {"id": player_id, "name": name}


def parse_search_payload(payload: object, *, requested_limit=LEGACY_SEARCH_LIMIT, query=None) -> SearchResults:
    """Validate the observed {players:[{id,name,...}], count:n} API schema.

    A malformed row fails the response instead of being silently dropped: it
    could have been another exact-name candidate and change the identity result.
    The default remains the old 20-row request for saved/legacy callers.
    Production search_players explicitly passes its actual 50-row limit.
    """
    requested_limit = _limit(requested_limit)
    if not isinstance(payload, dict) or not isinstance(payload.get("players"), list):
        raise PlayerSearchError("search response must contain a players array")
    rows = payload["players"]
    count = payload.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count != len(rows):
        raise PlayerSearchError("search count is missing or inconsistent; candidates may be incomplete")
    if len(rows) > requested_limit:
        raise PlayerSearchError("search response exceeds its requested candidate limit")
    return SearchResults([_candidate(row) for row in rows], requested_limit=requested_limit, query=query)


def search_players(name: str, timeout: float = 4.0) -> SearchResults:
    """Return public identity candidates only; never choose a player by rank.

    Recommended integration: ``gate.run(search_players, name, timeout=4.0)``.
    One HTTP request asks for 50 candidates. A full 50-row response remains
    incomplete; there is no pagination or hidden follow-up request. Preserve
    SearchResults metadata when combining responses for several OCR variants.
    """
    query = normalize_name(name)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    url = SEARCH_URL + "?" + urlencode({"q": query, "limit": SEARCH_LIMIT})
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=float(timeout)) as response:
        length = response.headers.get("Content-Length")
        if length is not None:
            try:
                declared = int(length)
            except (ValueError, TypeError) as exc:
                raise PlayerSearchError("invalid Content-Length") from exc
            if declared < 0 or declared > MAX_RESPONSE_BYTES:
                raise PlayerSearchError("search response exceeds the size limit")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PlayerSearchError("search response exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise PlayerSearchError("search response is not valid UTF-8 JSON") from exc
    return parse_search_payload(payload, requested_limit=SEARCH_LIMIT, query=query)


def _batch_metadata(rows, queried_name=None):
    """Validate provenance or conservatively fall back to the legacy 20 cap."""
    if not isinstance(rows, (list, tuple)):
        raise PlayerSearchError("query candidates must be a list")
    required = ("requested_limit", "returned_count", "complete")
    if not isinstance(rows, SearchResults) or not all(hasattr(rows, key) for key in required):
        return {"query": queried_name, "requested_limit": LEGACY_SEARCH_LIMIT,
                "returned_count": len(rows), "complete": len(rows) < LEGACY_SEARCH_LIMIT,
                "completeness_basis": "legacy_limit"}
    limit = _limit(rows.requested_limit)
    if type(rows.returned_count) is not int or rows.returned_count != len(rows) or len(rows) > limit:
        raise PlayerSearchError("candidate count metadata is inconsistent")
    if type(rows.complete) is not bool or rows.complete != (len(rows) < limit):
        raise PlayerSearchError("candidate completeness metadata is inconsistent")
    query = getattr(rows, "query", None)
    if query is not None:
        query = normalize_name(query)
        if queried_name is not None and query != queried_name:
            raise PlayerSearchError("candidate query metadata does not match the requested variant")
    return {"query": query if query is not None else queried_name, "requested_limit": limit,
            "returned_count": rows.returned_count, "complete": rows.complete,
            "completeness_basis": "below_requested_limit" if rows.complete else "at_requested_limit"}


def resolve_exact_name(ocr_variants, candidates, *, query_results=None) -> dict:
    """Resolve only one distinct ID exactly matching a normalized OCR variant.

    Returns ``status`` (resolved/ambiguous/unmatched/invalid/incomplete),
    ``player_id``, ``name``, ``matches``, ``candidates``,
    ``normalized_variants`` and a human-readable ``reason``. Only resolved has
    a player_id/name. ``candidates`` remain hints for explicit user review.

    Pass the full candidate list, not a prefiltered list of exact matches.
    When merging requests, query_results must contain (queried_variant, rows)
    for each normalized required variant exactly once, and candidates must be
    their unchanged concatenation. Completeness is checked per response, never
    by the length of the merged list. Bare legacy lists retain the 20-row cap.
    Never drop failed/truncated requests and treat the remainder as exhaustive.
    The caller must also refuse automatic identity confirmation after any
    required search fails. An exact nickname remains a name match, not proof
    that OCR captured the correct row or team from the game screen.
    """
    result = {"status": "invalid", "player_id": None, "name": None,
              "matches": [], "candidates": [], "normalized_variants": [], "queries": [], "reason": ""}
    try:
        if isinstance(ocr_variants, str):
            ocr_variants = [ocr_variants]
        if not isinstance(ocr_variants, (list, tuple)) or not ocr_variants:
            raise ValueError("OCR variants must contain at least one name")
        variants = list(dict.fromkeys(normalize_name(name) for name in ocr_variants))
        result["normalized_variants"] = variants
        if not isinstance(candidates, (list, tuple)):
            raise ValueError("candidates must be a complete list")
        validated = [_candidate(candidate) for candidate in candidates]
        coverage_issue = False
        if query_results is None:
            metadata = _batch_metadata(candidates)
            result["queries"] = [metadata]
            # A tagged single response cannot stand in for other required OCR
            # spellings. Untagged legacy complete-list calls remain compatible.
            if metadata["query"] is not None:
                coverage_issue = set(variants) != {metadata["query"]}
        else:
            if not isinstance(query_results, (list, tuple)):
                raise ValueError("query_results must contain every successful request")
            combined, queried = [], []
            for entry in query_results:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    raise ValueError("each query result must be (variant, candidates)")
                query, rows = entry
                query = normalize_name(query)
                metadata = _batch_metadata(rows, query)
                result["queries"].append(metadata)
                queried.append(query)
                combined.extend(_candidate(candidate) for candidate in rows)
            if combined != validated:
                raise ValueError("merged candidates differ from complete query results")
            coverage_issue = set(queried) != set(variants) or len(queried) != len(set(queried))
    except ValueError as exc:
        result["reason"] = "姓名或候选数据无效：" + str(exc)
        return result

    # Deduplicate repeated search hits without collapsing distinct player IDs.
    unique = {}
    for candidate in validated:
        key = (candidate["id"], normalize_name(candidate["name"]))
        unique.setdefault(key, candidate)
    result["candidates"] = sorted(unique.values(), key=lambda item: (item["id"], item["name"]))
    matches = [candidate for candidate in result["candidates"]
               if normalize_name(candidate["name"]) in variants]
    result["matches"] = matches
    ids = {candidate["id"] for candidate in matches}
    if len(ids) > 1:
        result.update(status="ambiguous", reason="完全名称对应多个玩家 ID，必须确认身份，不能按排名或第一项选择。")
    elif coverage_issue:
        result.update(status="incomplete", reason="尚未恰好取得每个必需昵称变体的查询结果，暂不自动认人。")
    elif any(not batch["complete"] for batch in result["queries"]):
        result.update(status="incomplete", reason="至少一次姓名查询达到候选数量上限，无法确认完整候选范围，暂不自动认人。")
    elif len(ids) == 1:
        match = matches[0]  # All matches have the same identity, not a ranked choice.
        result.update(status="resolved", player_id=match["id"], name=match["name"],
                      reason="在已取得的完整候选中，规范化后的完全名称唯一对应一个玩家 ID。")
    else:
        result.update(status="unmatched", reason="没有完全名称匹配；相近候选仅供确认，不自动模糊认人。")
    return result
