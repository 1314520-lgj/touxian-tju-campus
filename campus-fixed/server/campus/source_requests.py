"""Student source requests; shared collection remains an operator-controlled boundary."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Query, Request

from .platform import APIError, require_user
from .schema import now_iso, uid, validate
from .sources import FetchError, _get, _idempotency, _page, canonical_url


def normalized(body):
    result = {**body, "name": body["name"].strip()}
    if not result["name"]:
        raise APIError(422, "NAME_REQUIRED", "请填写网站或公众号名称")
    raw = (body["url"] or "").strip()
    if body["kind"] == "website" and not raw:
        raise APIError(422, "URL_REQUIRED", "网站需要公开栏目或订阅地址")
    result["url"] = None
    if raw:
        try:
            url = canonical_url(raw)
        except FetchError:
            raise APIError(422, "UNSAFE_URL", "请填写公开 HTTP/HTTPS 地址，不包含账号或密码") from None
        parts = urlsplit(url)
        host = parts.hostname
        if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
            raise APIError(422, "UNSAFE_URL", "不能申请接入本机或内网地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise APIError(422, "UNSAFE_URL", "不能申请接入本机或内网地址")
        if re.search(r"(?:[?&])(?:token|access_token|password|api_key|auth)=", url, re.I):
            raise APIError(422, "SECRET_IN_URL", "不要提交包含访问凭据的地址")
        if body["kind"] == "wechat":
            if (
                parts.scheme != "https"
                or host != "mp.weixin.qq.com"
                or not (parts.path == "/s" or parts.path.startswith("/s/"))
            ):
                raise APIError(422, "WECHAT_ARTICLE_REQUIRED", "公众号链接请使用微信公开文章地址；也可只填写名称")
            # Keep public article identity, dropping session and tracking parameters.
            query = urlencode(
                [
                    (k, v)
                    for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if k in {"__biz", "mid", "idx", "sn"}
                ]
            )
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
        result["url"] = url
    return result


class SourceRequests:
    def __init__(self, core):
        self.core, self.store = core, core.store

    @staticmethod
    def digest(body):
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def create(self, user_id, body, key):
        validate("SourceRequestCreate", body)
        body = normalized(body)
        fingerprint = self.digest(body)
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        receipt = f"source-request:create:{user_id}:{key_hash}"
        identity = f"{user_id}:{fingerprint}"
        now = now_iso()
        with self.store.transaction():
            # Lock before deduplication as well as counting: concurrent PostgreSQL
            # requests must see the first committed receipt/index. Replays use no quota.
            gate_revision = self.store.revision("source_request_limits", user_id)
            self.store.put("source_request_limits", user_id, {"updated_at": now}, expected_revision=gate_revision)
            previous = self.store.get("source_request_receipts", receipt)
            if previous:
                if previous["body_hash"] != fingerprint:
                    raise APIError(409, "IDEMPOTENCY_CONFLICT", "相同请求标识对应不同申请")
                return _get(self.core, "source_requests", previous["request_id"])
            index = self.store.get("source_request_indexes", identity)
            current = self.store.get("source_requests", index["request_id"]) if index else None
            if current and current["status"] in {"pending", "connected"}:
                self.store.put(
                    "source_request_receipts",
                    receipt,
                    {"body_hash": fingerprint, "request_id": current["id"]},
                    expected_revision=0,
                )
                return current
            cutoff = datetime.now(UTC) - timedelta(days=1)
            recent = [
                r
                for r in self.store.list("source_requests")
                if r["user_id"] == user_id and datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) > cutoff
            ]
            if len(recent) >= 10:
                raise APIError(429, "REQUEST_LIMIT", "每天最多提交10个新来源，请等待已有申请处理")
            record = {
                "id": uid(),
                "user_id": user_id,
                **body,
                "status": "pending",
                "source_id": None,
                "resolution_note": None,
                "created_at": now,
                "updated_at": now,
            }
            validate("SourceRequest", record)
            self.store.put("source_requests", record["id"], record, expected_revision=0)
            self.store.put(
                "source_request_indexes",
                identity,
                {"request_id": record["id"]},
                expected_revision=self.store.revision("source_request_indexes", identity),
            )
            self.store.put(
                "source_request_receipts",
                receipt,
                {"body_hash": fingerprint, "request_id": record["id"]},
                expected_revision=0,
            )
            return record

    def list(self, user_id=None):
        return sorted(
            [r for r in self.store.list("source_requests") if user_id is None or r["user_id"] == user_id],
            key=lambda r: r["created_at"],
            reverse=True,
        )

    def resolve(self, actor_id, request_id, body, key):
        validate("SourceRequestResolve", body)
        body = {**body, "note": body["note"].strip()}
        if not body["note"]:
            raise APIError(422, "NOTE_REQUIRED", "请填写接入核对或未接入的原因")
        if body["decision"] == "connect" and not body["source_id"]:
            raise APIError(422, "SOURCE_REQUIRED", "请选择已接通的信息来源")
        if body["decision"] == "reject" and body["source_id"] is not None:
            raise APIError(422, "SOURCE_UNEXPECTED", "未接入的申请不能绑定来源")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        receipt = f"source-request:resolve:{actor_id}:{request_id}:{key_hash}"
        fingerprint = self.digest(body)
        with self.store.transaction():
            previous = self.store.get("source_request_receipts", receipt)
            if previous:
                if previous["body_hash"] != fingerprint:
                    raise APIError(409, "IDEMPOTENCY_CONFLICT", "相同请求标识对应不同操作")
                return _get(self.core, "source_requests", request_id)
            revision = self.store.revision("source_requests", request_id)
            record = _get(self.core, "source_requests", request_id)
            if record["status"] != "pending":
                raise APIError(409, "REQUEST_RESOLVED", "此申请已处理，请刷新列表")
            if body["decision"] == "connect":
                source = _get(self.core, "sources", body["source_id"])
                if (
                    not source["enabled"]
                    or source["health"] != "healthy"
                    or not source["last_success_at"]
                    or source["verification"] not in {"verified_official", "verified_organization"}
                ):
                    raise APIError(409, "SOURCE_NOT_READY", "来源需先启用、核验身份，并完成一次成功采集")
                if record["kind"] == "wechat" and source["kind"] != "wechat":
                    raise APIError(409, "SOURCE_KIND_MISMATCH", "公众号申请应连接公众号来源")
                if record["kind"] == "website" and source["kind"] == "wechat":
                    raise APIError(409, "SOURCE_KIND_MISMATCH", "网站申请应连接网站或订阅来源")
                if record["auto_subscribe"]:
                    sub_revision = self.store.revision("subscriptions", record["user_id"])
                    subscriptions = self.store.get("subscriptions", record["user_id"]) or {
                        "source_ids": [],
                        "event_types": [],
                    }
                    # All-source subscribers already follow this source. Explicit empty selection remains distinct.
                    mode = subscriptions.get("source_mode", "selected" if subscriptions["source_ids"] else "all")
                    if mode == "selected" and source["id"] not in subscriptions["source_ids"]:
                        subscriptions = {**subscriptions, "source_ids": [*subscriptions["source_ids"], source["id"]]}
                        validate("Subscriptions", subscriptions)
                        self.store.put(
                            "subscriptions", record["user_id"], subscriptions, expected_revision=sub_revision
                        )
                        self.store.emit(
                            "subscriptions.changed",
                            record["user_id"],
                            self.store.revision("subscriptions", record["user_id"]),
                            user_id=record["user_id"],
                        )
            record.update(
                status="connected" if body["decision"] == "connect" else "rejected",
                source_id=body["source_id"],
                resolution_note=body["note"],
                updated_at=now_iso(),
            )
            validate("SourceRequest", record)
            self.store.put("source_requests", request_id, record, expected_revision=revision)
            self.store.put(
                "source_request_receipts",
                receipt,
                {"body_hash": fingerprint, "request_id": request_id},
                expected_revision=0,
            )
            return record


def make_router(core):
    router = APIRouter()

    @router.get("/me/source-requests")
    def mine(request: Request, cursor: str | None = None, limit: int = Query(20, ge=1, le=100)):
        user = require_user(request)
        return _page(core.source_requests.list(user["id"]), cursor, limit)

    @router.post("/me/source-requests", status_code=201)
    async def create(request: Request):
        user = require_user(request, write=True)
        return core.source_requests.create(user["id"], await request.json(), _idempotency(request))

    @router.get("/admin/source-requests")
    def inventory(request: Request, cursor: str | None = None, limit: int = Query(20, ge=1, le=100)):
        require_user(request, roles={"admin", "operator"})
        return _page(core.source_requests.list(), cursor, limit)

    @router.post("/admin/source-requests/{request_id}/resolve")
    async def resolve(request_id: str, request: Request):
        user = require_user(request, roles={"admin", "operator"}, write=True)
        return core.source_requests.resolve(user["id"], request_id, await request.json(), _idempotency(request))

    return router
