from __future__ import annotations

import hashlib
import os
import re
import time
from collections import defaultdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Body, Query, Request

from .events import page
from .platform import APIError, require_user
from .schema import now_iso, validate
from .zh_index import LocalTextIndex

# Curated near-synonyms for campus wording. Deliberately small: only pairs that
# mean the same thing to a student, never merely related topics. Expansion is
# additive, so a query that already matches is unaffected.
SYNONYM_GROUPS = (
    ("截止", "截至", "截止日期"),
    ("报名", "申请", "申报"),
    ("材料", "资料"),
    ("成绩", "分数", "绩点"),
    ("奖学金", "奖助学金", "助学金"),
    ("补考", "重考"),
    ("宿舍", "公寓", "住宿"),
    ("选课", "课程选择"),
    ("考试", "考核", "测验"),
    ("缴费", "交费", "收费"),
    ("答辩", "论文答辩"),
    ("实习", "实践"),
    ("毕业", "毕业论文", "毕业设计"),
    ("体检", "健康检查"),
    ("退课", "撤课"),
)
SYNONYMS = {word: [other for other in group if other != word] for group in SYNONYM_GROUPS for word in group}
# Below this many distinct events, a literal query is considered too narrow.
SYNONYM_RETRY = 2

# Excerpt window. The quote handed to the client is also registered as evidence
# and verified by substring, so it may be trimmed but never rewritten: these
# numbers only move the two cut points.
EXCERPT_LEAD = 60  # characters of run-up kept before the match
EXCERPT_WINDOW = 500  # target length of the quote
EXCERPT_OVERSHOOT = 80  # how far past the target a sentence end may be chased
# A quote that stops mid-sentence reads as if the notice ended there.
BREAK_AFTER = "。！？；\n"

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _deadline_passed(deadline, now):
    """Same truth as the front-end ``feedView.deadlineHasPassed()``.

    A date-only deadline has passed once the school-day date is earlier than
    today in Asia/Shanghai; a minute deadline has passed once its instant is
    earlier than ``now``. Malformed or uncertain values never count as passed —
    a missing deadline must stay visible for the student to check, which is the
    same rule the feed uses to fold "已截止" cards.
    """
    if deadline.get("precision") == "date":
        date_value = deadline.get("date") or ""
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value)) and date_value < now.astimezone(
            SHANGHAI_TZ
        ).date().isoformat()
    if deadline.get("precision") == "minute":
        at = deadline.get("at")
        if not at:
            return False
        try:
            parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed < now
    return False


def _event_ended(event, now=None):
    """Mirror of the front-end ``feedView.eventHasEnded()``.

    A published event is treated as ended when it is cancelled, or when every
    closing deadline (all kinds except ``event_time``; fall back to all when
    none is a closing one) has passed. This is exactly the predicate the feed
    uses to fold a card into "已截止或取消", so a search hit can carry the same
    verdict instead of showing an expired deadline as if it were still open.
    """
    if event.get("status") == "cancelled":
        return True
    now = now if now is not None else datetime.now(UTC)
    deadlines = event.get("deadlines") or []
    closing = [d for d in deadlines if d.get("kind") != "event_time"]
    relevant = closing if closing else deadlines
    return bool(relevant) and all(_deadline_passed(d, now) for d in relevant)


class Search:
    def __init__(self, core):
        self.core, self.store = core, core.store
        self.url = os.getenv("CAMPUS_MEILI_URL", "").rstrip("/")
        self.key = os.getenv("CAMPUS_MEILI_KEY", "")
        self.index = "tju-public-segments"
        self.local = LocalTextIndex(self.store)

    def _http(self, method, path, body=None):
        try:
            with httpx.Client(timeout=8, trust_env=False) as client:
                response = client.request(
                    method,
                    self.url + path,
                    json=body,
                    headers={"Authorization": "Bearer " + self.key} if self.key else {},
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise APIError(503, "search_unavailable", "全文搜索服务暂不可用，请稍后重试") from None

    def _wait(self, task):
        task_id = task.get("taskUid")
        if task_id is None:
            return
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            status = self._http("GET", f"/tasks/{task_id}")
            if status["status"] == "succeeded":
                return
            if status["status"] in ["failed", "canceled"]:
                raise APIError(503, "index_failed", "索引任务失败，可在后台重试")
            time.sleep(0.15)
        raise APIError(503, "index_pending", "索引任务尚未完成，将重试确认")

    def index_event(self, payload):
        subject = payload["subject"]
        event = self.store.get("events", subject["id"])
        if not event or event["version"] != subject["revision"]:
            return
        if self.url:
            indexes = self._http("GET", "/indexes")
            if not any(x["uid"] == self.index for x in indexes.get("results", [])):
                self._wait(self._http("POST", "/indexes", {"uid": self.index, "primaryKey": "id"}))
            self._wait(
                self._http(
                    "PATCH",
                    f"/indexes/{self.index}/settings",
                    {
                        "searchableAttributes": ["title", "text"],
                        "filterableAttributes": ["event_id", "event_version", "college_ids"],
                        "distinctAttribute": "event_id",
                    },
                )
            )
            rows = []
            if event["publication_state"] == "published":
                for segment in self.core.pipeline.segments(self.core.events.current_snapshot_ids(event["id"])):
                    key = f"{event['id']}:{event['version']}:{segment['snapshot_id']}:{segment['id']}"
                    rows.append(
                        {
                            "id": hashlib.sha256(key.encode()).hexdigest(),
                            "event_id": event["id"],
                            "event_version": event["version"],
                            "snapshot_id": segment["snapshot_id"],
                            "segment_id": segment["id"],
                            "title": event["title"],
                            "text": segment["text"],
                            "page": segment["page"],
                            "college_ids": event["audience"]["college_ids"],
                        }
                    )
                if rows:
                    self._wait(self._http("POST", f"/indexes/{self.index}/documents", rows))
            comparison = "<=" if event["publication_state"] == "withdrawn" else "<"
            filter_text = f"event_id = '{event['id']}' AND event_version {comparison} {event['version']}"
            self._wait(self._http("POST", f"/indexes/{self.index}/documents/delete", {"filter": filter_text}))
        with self.store.transaction():
            current = self.store.get("events", event["id"])
            if current and current["version"] == event["version"]:
                self.store.put(
                    "search_receipts",
                    event["id"],
                    {"event_id": event["id"], "version": event["version"], "updated_at": now_iso()},
                )
                self._index_local(current)

    def _index_local(self, event):
        """Refresh the local FTS5 rows, then stamp the version they describe.

        The receipt is what lets the scan trust the index: a query may narrow an
        event only when the indexed version is the one being served.
        """
        if not self.local.supported:
            return
        try:
            # A withdrawn event has no readable captures, so it is removed by
            # having no rows rather than by asking for its segments.
            segments = (
                []
                if event["publication_state"] != "published"
                else list(self.core.pipeline.segments(self.core.events.current_snapshot_ids(event["id"])))
            )
            count = self.local.replace(event, segments)
            self.store.put(
                "local_index_receipts",
                event["id"],
                {"event_id": event["id"], "version": event["version"], "segments": count, "updated_at": now_iso()},
            )
        except Exception:
            # A search accelerator must never fail the indexing job that owns it.
            self.store.put(
                "local_index_receipts",
                event["id"],
                {"event_id": event["id"], "version": None, "segments": 0, "updated_at": now_iso()},
            )

    @staticmethod
    def terms(q):
        q = q.strip()
        if not q:
            raise APIError(422, "empty_query", "请输入查询关键词")
        normalized = re.sub(
            r"什么时候|什么时间|怎么办|需要哪些|需要什么|哪些材料|在哪里|哪里|截止日期|截止时间|是什么|怎么|何时|请问|一下|的|吗|呢|？|\?",
            " ",
            q.lower(),
        )
        terms = re.findall(r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}", normalized)
        chinese = [s for s in terms if re.search(r"[\u4e00-\u9fff]", s)]
        return list(dict.fromkeys(terms + [s[i : i + 2] for s in chinese for i in range(len(s) - 1)])) or [
            q.strip().lower()
        ]

    def _eligible(self, event, college_id=None, education_level=None):
        if event["publication_state"] != "published":
            return False
        audience = event["audience"]
        if college_id and audience["college_ids"] and college_id not in audience["college_ids"]:
            return False
        if (
            education_level
            and education_level != "unknown"
            and audience["education_levels"]
            and education_level not in audience["education_levels"]
        ):
            return False
        return True

    @staticmethod
    def _excerpt(text, terms):
        """A continuous, verifiable part of the original segment, around the match.

        A long full keyword is more useful than an early common two-character
        fragment, so the window is anchored on the longest query term the
        segment actually contains. When no term is in the body the match came
        from the event title alone, and the notice's opening is shown instead.

        Two boundaries are nudged so the quote does not mislead a reader:

        * it opens just after the previous sentence break, when that break is
          still close enough to keep the match inside;
        * it closes on the next sentence break within a small budget, or takes
          the short remainder whole, so it rarely stops mid-sentence.

        Both are pure index arithmetic on ``text``: the result is always a
        literal slice, which is what lets a caller register it as evidence.
        """
        lower = text.lower()
        matches = sorted((t for t in terms if t in lower), key=lambda t: -len(t))
        position = lower.find(matches[0]) if matches else 0
        start = max(0, position - EXCERPT_LEAD)
        previous = max(text.rfind(break_, start, position) for break_ in BREAK_AFTER)
        if previous >= start:
            start = previous + 1
        end = min(len(text), start + EXCERPT_WINDOW)
        if len(text) - end <= EXCERPT_OVERSHOOT:
            end = len(text)
        else:
            following = [at for at in (text.find(break_, end) for break_ in BREAK_AFTER) if at != -1]
            if following and min(following) + 1 - end <= EXCERPT_OVERSHOOT:
                end = min(following) + 1
        return text[start:end]

    @staticmethod
    def synonyms(terms):
        """Near-synonyms of the query terms, in order, never repeating a term."""
        expanded = []
        for term in terms:
            expanded.extend(SYNONYMS.get(term, ()))
        return [term for term in dict.fromkeys(expanded) if term not in terms]

    def _scan(self, versions, published, terms, college_id, education_level, include_historical, penalty=0):
        """Match every served paragraph by substring, optionally narrowed.

        The local FTS5 index may shrink the paragraphs visited, but only for an
        event whose indexed version is the one being served; anything else is
        scanned in full, so the index can never remove a match.
        """
        narrowed, receipts = None, {}
        if not include_historical:
            narrowed, receipts = self.local.lookup(terms)
        found = []
        for event in versions:
            current = published.get(event["id"])
            if not current or not self._eligible(event, college_id, education_level):
                continue
            allowed = None
            if narrowed is not None and receipts.get(event["id"]) == event["version"]:
                allowed = narrowed.get(event["id"]) or set()
                # A current index can reject an event before fetching snapshots
                # or copying parsed bodies. Stale/missing receipts still scan.
                if not allowed:
                    continue
            historical = event["version"] != current["version"]
            snapshot_ids = (
                self.core.events.snapshot_ids(event["id"], event["version"])
                if historical
                else self.core.events.current_snapshot_ids(event["id"])
            )
            segments = list(self.core.pipeline.segments(snapshot_ids))
            if allowed is not None:
                segments = [s for s in segments if (s["snapshot_id"], s["id"]) in allowed]
            title = event["title"].lower()
            # The score is deliberately small and explainable, because a reader
            # can be shown the same evidence the ranker used:
            #
            #   score = overlap with the paragraph + title overlap - synonym penalty
            #
            # "overlap" sums the length of every query term found, capped at 10,
            # so a long specific term outranks a short common one. Terms are
            # already expanded into their own bigrams by ``terms()``, so a
            # longer literal overlap contributes more of them. The title is
            # worth a flat 10 per term because it is the event's own summary.
            # BM25 is not used here on purpose — see ``zh_index`` for why.
            for segment in segments:
                text = segment["text"]
                haystack = (title + " " + text).lower()
                matches = [t for t in terms if t in haystack]
                if not matches:
                    continue
                score = sum(min(len(t), 10) for t in matches) + sum(10 for t in terms if t in title) - penalty
                quote = self._excerpt(text, terms)
                if quote.strip():
                    found.append((score, event, segment, quote, historical))
        return found

    def hits(self, q, college_id=None, education_level=None, include_historical=False):
        terms = self.terms(q)
        now = self.core.delivery.clock()
        candidates = []
        if college_id and college_id not in {c["id"] for c in self.store.get("catalog", "tju")["colleges"]}:
            raise APIError(422, "college_unknown", "学院不在学校目录中")
        published = {e["id"]: e for e in self.store.list("events") if e["publication_state"] == "published"}
        if self.url and not include_historical:
            result = self._http("POST", f"/indexes/{self.index}/search", {"q": q.strip(), "limit": 100})
            segment_cache = {}
            for rank, item in enumerate(result.get("hits", [])):
                event = published.get(item["event_id"])
                if (
                    not event
                    or item["event_version"] != event["version"]
                    or not self._eligible(event, college_id, education_level)
                ):
                    continue
                if event["id"] not in segment_cache:
                    segment_cache[event["id"]] = {
                        (s["snapshot_id"], s["id"]): s
                        for s in self.core.pipeline.segments(self.core.events.current_snapshot_ids(event["id"]))
                    }
                segment = segment_cache[event["id"]].get((item["snapshot_id"], item["segment_id"]))
                if not segment:
                    continue
                quote = self._excerpt(segment["text"], terms)
                if quote.strip():
                    candidates.append((100 - rank / 1000, event, segment, quote, False))
        else:
            versions = self.store.list("event_versions") if include_historical else published.values()
            candidates = self._scan(versions, published, terms, college_id, education_level, include_historical)
            # A query worded differently from the notice finds nothing literal.
            # Only then widen it with curated near-synonyms, and rank those
            # below every literal match so an existing result is never displaced.
            extra = self.synonyms(terms)
            if extra and len({row[1]["id"] for row in candidates}) < SYNONYM_RETRY:
                candidates += self._scan(
                    versions, published, terms + extra, college_id, education_level, include_historical, penalty=1
                )
        candidates.sort(key=lambda h: (-h[0], h[4], h[1]["title"]))
        # Preserve several relevant proof candidates for answers, without writing
        # evidence for every matching paragraph or filling search with one notice.
        hits = []
        per_version = defaultdict(int)
        seen = set()
        for score, event, segment, quote, historical in candidates:
            identity = (event["id"], event["version"])
            proof_key = (identity, segment["snapshot_id"], segment["id"], quote)
            if per_version[identity] >= 3 or proof_key in seen:
                continue
            seen.add(proof_key)
            per_version[identity] += 1
            evidence = self.core.pipeline.register_evidence(segment["snapshot_id"], segment["id"], quote)
            hits.append(
                (
                    score,
                    {
                        "event_id": event["id"],
                        "event_version": event["version"],
                        "title": event["title"],
                        "excerpt": quote,
                        "source_url": evidence["source_url"],
                        "evidence_id": evidence["id"],
                        "historical": historical,
                        "ended": _event_ended(event, now),
                    },
                    evidence,
                    event,
                )
            )
        return hits

    def _index_status(self):
        """``current`` / ``lagging`` for whichever index actually serves queries.

        A query is answered correctly either way: an event whose indexed version
        is not the one being served is scanned in full, so a stale accelerator
        can never remove a hit. What it can do is make search slower, and that
        is worth telling the caller rather than hiding.

        Only the remote index used to be checked, so a local-only deployment —
        which is the shipped default, with no Meilisearch configured — reported
        ``current`` even while its own FTS5 rows were behind. Withdrawn events
        are excluded: the index intentionally holds no rows for them, and a
        receipt is still stamped when they are removed.
        """
        published = [e for e in self.store.list("events") if e["publication_state"] == "published"]
        if self.url and any(
            (self.store.get("search_receipts", e["id"]) or {}).get("version") != e["version"]
            for e in self.store.list("events")
        ):
            return "lagging"
        if self.local.supported:
            receipts = self.local.receipts()
            if any(receipts.get(e["id"]) != e["version"] for e in published):
                return "lagging"
        return "current"

    def search(self, q, college_id=None, include_historical=False, cursor=None, limit=20):
        rows = self.hits(q, college_id=college_id, include_historical=include_historical)
        # Final publication/version gate also protects races while evidence is registered.
        safe = []
        seen = set()
        current_versions = {}
        for hit in rows:
            event_id, version = hit[1]["event_id"], hit[1]["event_version"]
            if (event_id, version) in seen:
                continue
            if event_id not in current_versions:
                try:
                    current_versions[event_id] = self.core.events.get_public(event_id)["version"]
                except APIError:
                    current_versions[event_id] = None
            current = current_versions[event_id]
            if current is not None and (include_historical or current == version):
                safe.append(hit[1])
                seen.add((event_id, version))
        paged = page(safe, cursor, limit)
        response = {"hits": paged["items"], "next_cursor": paged["next_cursor"], "index_status": self._index_status()}
        validate("SearchResponse", response)
        return response

    def answer(self, user_id, body):
        validate("AnswerRequest", body)
        profile = self.store.get("profiles", user_id) or {}
        rows = self.hits(
            body["question"],
            body["college_id"] or profile.get("college_id"),
            body["education_level"] or profile.get("education_level"),
            body["include_historical"],
        )
        citations = []
        parts = []
        seen = set()
        uncertain = False
        skipped_conflicts = []
        date_question = any(
            word in body["question"].lower() for word in ["截止", "什么时候", "何时", "ddl", "报名时间"]
        )
        if body["include_historical"] and date_question:
            timelines = defaultdict(dict)
            for _, _, _, event in rows:
                timelines[event["id"]][event["version"]] = tuple(
                    self.core.events._semantic("deadlines", event["deadlines"])
                )
            if any(len(set(versions.values())) > 1 for versions in timelines.values()):
                for event_id in timelines:
                    self.core.events.get_public(event_id)
                return {
                    "status": "conflicting_sources",
                    "text": "历史版本与当前版本的期限不同，请关闭历史查询后核对当前办理期限。",
                    "citations": [],
                    "generated_at": now_iso(),
                }

        def cite(event, evidence):
            if any(c["evidence_id"] == evidence["id"] and c["event_version"] == event["version"] for c in citations):
                return
            citations.append(
                {
                    "event_id": event["id"],
                    "event_version": event["version"],
                    "evidence_id": evidence["id"],
                    "source_url": evidence["source_url"],
                    "quote": evidence["quote"],
                    "page": evidence["page"],
                }
            )

        material_question = any(w in body["question"] for w in ["材料", "提交什么", "准备什么"])
        entry_question = any(w in body["question"] for w in ["入口", "在哪", "哪里", "链接", "报名地址"])
        for _, _, proof, event in rows:
            identity = (event["id"], event["version"])
            if identity in seen:
                continue
            seen.add(identity)
            if event["status"] == "under_review":
                uncertain = True
                continue
            evidences = {e["id"]: e for e in event["evidence"]}
            sections = []

            def supported(refs):
                return [evidences[eid] for eid in refs if eid in evidences]

            def add(text, proofs, missing):
                nonlocal uncertain
                if proofs:
                    sections.append(text)
                    for evidence in proofs:
                        cite(event, evidence)
                else:
                    uncertain = True
                    sections.append(missing)

            if event["status"] == "cancelled":
                add("事项已取消。", supported(event["field_evidence"]["status"]), "未找到可核对的取消依据。")
            elif date_question or material_question or entry_question:
                if date_question:
                    dates = [d for d in event["deadlines"] if d["precision"] != "unknown"]
                    # A real conflict is one *purpose* stated with two different
                    # dates. The purpose is what remains of the quote once the
                    # date itself is taken out: "报名截止时间：2026年9月24日" and
                    # "报名截止时间：2026年9月28日" share a sentence skeleton and
                    # contradict each other, while 报名截止 / 考试 / 报送材料 are
                    # different subjects that merely happen to be the same kind.
                    # Grouping by kind alone made the real 四六级 notice report a
                    # conflict instead of its deadlines.
                    def skeleton(text):
                        return re.sub(r"[\d年月日时分秒:：\-—~～\.\s（）()]+", "·", text or "")

                    by_purpose = defaultdict(set)
                    for d in dates:
                        purpose = skeleton(d.get("raw_text"))
                        if purpose.strip("·"):
                            by_purpose[purpose].add((d["precision"], d.get("date"), d.get("at")))
                    if any(len(values) > 1 for values in by_purpose.values()):
                        # Skip this notice, do not blank the answer: other hits may
                        # answer the question cleanly, and returning here threw
                        # them all away. The skip is reported so the student knows
                        # one source was ambiguous.
                        skipped_conflicts.append(event["title"])
                        continue
                    labels = {
                        "school_deadline": "学校期限",
                        "college_deadline": "学院期限",
                        "event_time": "活动时间",
                        "other": "原文时间",
                    }
                    date_text = "；".join(
                        dict.fromkeys(
                            labels[d["kind"]]
                            + " "
                            + (d["date"] + "（未提供具体时刻）" if d["precision"] == "date" else d["at"])
                            for d in dates
                        )
                    )
                    proofs = supported([eid for d in dates for eid in d["evidence_ids"]])
                    add(date_text, proofs if dates else [], "未找到可核对的时间依据。")
                if material_question:
                    proofs = supported(event["field_evidence"]["materials"])
                    add(
                        "材料要求：" + "、".join(event["materials"]),
                        proofs if event["materials"] else [],
                        "未找到可核对的材料要求。",
                    )
                if entry_question:
                    proofs = supported(event["field_evidence"]["entry_urls"])
                    add(
                        "办理入口：" + "；".join(event["entry_urls"]),
                        proofs if event["entry_urls"] else [],
                        "未找到可核对的办理入口。",
                    )
            else:
                sections.append("原文相关段落如下。")
                cite(event, proof)
            if sections:
                parts.append(event["title"] + "：" + "；".join(sections))
            if len(parts) >= 3:
                break
        # Missing-field messages also contain an event title; gate them after
        # assembly just like proven statements and never expose withdrawn data.
        for event_id, version in seen:
            current = self.core.events.get_public(event_id)
            if not body["include_historical"] and current["version"] != version:
                raise APIError(409, "answer_changed", "资料已更新，请重新查询")
        conflict_note = (
            "另有 "
            + "、".join(dict.fromkeys(skipped_conflicts))
            + " 的原文期限存在冲突（同一说法给出不同日期），未采纳，请自行核对。"
            if skipped_conflicts
            else ""
        )
        if not citations:
            text = "\n".join(parts) if parts else "未找到可核对的办理依据。"
            if conflict_note:
                text = (text + "\n" + conflict_note).strip()
            else:
                text = text + (
                    "匹配内容仍有日期或对象待核对。" if uncertain else "可尝试通知标题、项目名或更具体关键词。"
                )
            return {
                "status": "conflicting_sources" if skipped_conflicts else "insufficient_evidence",
                "text": text,
                "citations": [],
                "generated_at": now_iso(),
            }
        if conflict_note:
            parts = parts + [conflict_note]
        result = {"status": "grounded", "text": "\n".join(parts), "citations": citations, "generated_at": now_iso()}
        validate("Answer", result)
        return result


def make_router(core):
    router = APIRouter()

    @router.get("/search")
    def search(
        q: str = Query(..., min_length=1, max_length=500),
        college_id: str | None = None,
        include_historical: bool = False,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ):
        return core.search.search(q, college_id, include_historical, cursor, limit)

    @router.post("/answers")
    def answer(request: Request, body: dict = Body(...)):
        user = require_user(request, write=True)
        try:
            return core.search.answer(user["id"], body)
        except APIError as exc:
            if exc.status == 503:
                return {
                    "status": "temporarily_unavailable",
                    "text": "检索服务暂不可用，请稍后重试。",
                    "citations": [],
                    "generated_at": now_iso(),
                }
            raise

    return router
