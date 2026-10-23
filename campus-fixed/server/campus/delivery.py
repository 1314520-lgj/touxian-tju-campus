from __future__ import annotations

import copy
import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Query, Request, Response

from .events import page
from .platform import APIError, require_user
from .schema import now_iso, uid, validate

TZ = ZoneInfo("Asia/Shanghai")
# Channels that reach a person outside the app, and therefore respect quiet hours.
QUIET_CHANNELS = {"email", "push"}
DEFAULTS = {
    "daily_digest_enabled": True,
    "digest_time": "08:00",
    "channels": ["in_app"],
    "quiet_start": "22:00",
    "quiet_end": "08:00",
    "timezone": "Asia/Shanghai",
    "date_only_reminder_time": "09:00",
    "reminders_enabled": True,
    "reminder_lead_minutes": 1440,
    "change_reminders_enabled": True,
    "calendar_enabled": True,
}


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Delivery:
    def __init__(self, core, clock=None):
        self.core, self.store, self.clock = (
            core,
            core.store,
            clock or (lambda: datetime.now(UTC)),
        )

    def preferences(self, user_id):
        return self.store.get("preferences", user_id) or copy.deepcopy(DEFAULTS)

    def update_preferences(self, user_id, body):
        validate("DeliveryPreferences", body)
        if "push" in body["channels"] and not self.core.push.status(user_id)["available"]:
            raise APIError(
                409,
                "PUSH_NOT_READY",
                "个人推送尚未安全绑定，请联系维护者或选择其他提醒渠道",
            )
        with self.store.transaction():
            self.store.put("preferences", user_id, body)
            self.store.emit(
                "delivery_preferences.changed",
                user_id,
                self.store.revision("preferences", user_id),
                user_id=user_id,
            )
        return body

    def _quiet(self, when, prefs):
        if not prefs["quiet_start"] or prefs["quiet_start"] == prefs["quiet_end"]:
            return when
        local = when.astimezone(TZ)
        minute = local.strftime("%H:%M")
        start, end = prefs["quiet_start"], prefs["quiet_end"]
        inside = start <= minute < end if start < end else minute >= start or minute < end
        if not inside:
            return when
        eh, em = map(int, end.split(":"))
        result = local.replace(hour=eh, minute=em, second=0, microsecond=0)
        if result <= local:
            result += timedelta(days=1)
        return result.astimezone(UTC)

    def _deadlines(self, resolved):
        record = resolved["record"]
        if not record:
            return []
        if resolved["subject"]["kind"] == "public_event":
            return record["deadlines"]
        draft = record["draft"]
        if draft["deadline_at"]:
            return [
                {
                    "kind": "other",
                    "precision": "minute",
                    "at": draft["deadline_at"],
                    "date": None,
                }
            ]
        if draft["deadline_date"]:
            return [
                {
                    "kind": "other",
                    "precision": "date",
                    "at": None,
                    "date": draft["deadline_date"],
                }
            ]
        return []

    def _calendar(self, user_id, resolved, prefs):
        subject = resolved["subject"]
        key = f"{user_id}:{subject['kind']}:{subject['id']}"
        old = self.store.get("calendar_entries", key)
        record = resolved["record"]
        cancelled = (
            not prefs["calendar_enabled"]
            or not resolved["selected"]
            or resolved["deleted"]
            or resolved["withdrawn"]
            or bool(record and record.get("status") == "cancelled")
        )
        deadlines = [d for d in self._deadlines(resolved) if d["precision"] != "unknown"]
        first = min(deadlines, key=lambda d: d.get("date") or d.get("at")) if deadlines else None
        if not old and (cancelled or first is None):
            return
        if first is None:
            cancelled = True
        title = (
            "事项已取消"
            if cancelled
            else record["title"]
            if subject["kind"] == "public_event"
            else record["draft"]["title"]
        )
        actions = (
            []
            if cancelled
            else record["action_steps"]
            if subject["kind"] == "public_event"
            else record["draft"]["action_steps"]
        )
        urls = (
            []
            if cancelled
            else record["entry_urls"]
            if subject["kind"] == "public_event"
            else record["draft"]["entry_urls"]
        )
        start_date = (old or {}).get("start_date") if cancelled else (first or {}).get("date")
        start_at = (old or {}).get("start_at") if cancelled else (first or {}).get("at")
        shape = {
            "title": title,
            "description": "\n".join(actions + urls),
            "start_date": start_date,
            "start_at": start_at,
            "status": "CANCELLED" if cancelled else "CONFIRMED",
        }
        fingerprint = digest(shape)
        with self.store.transaction():
            latest = self.store.get("calendar_entries", key)
            sequence = (latest or {}).get("sequence", 0) + (0 if latest and latest["fingerprint"] == fingerprint else 1)
            entry = {
                "user_id": user_id,
                "subject": subject,
                "uid": hmac.new(self.core.settings.secret.encode(), key.encode(), hashlib.sha256).hexdigest()
                + "@touxian.local",
                "sequence": sequence,
                "dtstamp": stamp(self.clock()),
                "fingerprint": fingerprint,
                **shape,
            }
            self.store.put("calendar_entries", key, entry)

    def _plan(self, user_id, subject, kind, semantic, when, prefs, desired, cutoff=None):
        for channel in prefs["channels"]:
            effective = self._quiet(when, prefs) if channel in QUIET_CHANNELS else when
            if cutoff and effective >= cutoff and channel in QUIET_CHANNELS:
                continue
            base = {
                "user_id": user_id,
                "kind": kind,
                "subject_kind": subject["kind"],
                "subject_id": subject["id"],
                "semantic": semantic,
                "channel": channel,
            }
            family = digest(base)
            key = digest(
                {
                    **base,
                    "scheduled": stamp(when),
                    "quiet_start": prefs["quiet_start"],
                    "quiet_end": prefs["quiet_end"],
                }
            )
            with self.store.transaction():
                family_revision = self.store.revision("delivery_families", family)
                family_row = self.store.get("delivery_families", family)
                family_delivery = self.store.get("deliveries", family_row["delivery_id"]) if family_row else None
                if family_delivery and family_delivery["status"] in [
                    "sending",
                    "sent",
                    "unknown",
                ]:
                    if family_row.get("plan_id"):
                        desired.add(family_row["plan_id"])
                    continue
                previous = self.store.get("reminder_plans", key)
                delivery = self.store.get("deliveries", previous["delivery_id"]) if previous else None
                if delivery and delivery["status"] in ["sending", "sent", "unknown"]:
                    desired.add(key)
                    continue
                identifier = delivery["id"] if delivery and delivery["status"] != "cancelled" else uid()
                status = "failed" if delivery and delivery["status"] == "failed" else "scheduled"
                delivery = {
                    "id": identifier,
                    "user_id": user_id,
                    "subject": subject,
                    "channel": channel,
                    "kind": kind,
                    "status": status,
                    "scheduled_at": stamp(effective),
                    "sent_at": None,
                }
                validate("Delivery", delivery)
                self.store.put(
                    "delivery_families",
                    family,
                    {"delivery_id": identifier, "plan_id": key},
                    expected_revision=family_revision,
                )
                self.store.put("deliveries", identifier, delivery)
                self.store.put(
                    "reminder_plans",
                    key,
                    {
                        "id": key,
                        "user_id": user_id,
                        "subject": subject,
                        "kind": kind,
                        "family": family,
                        "semantic": semantic,
                        "delivery_id": identifier,
                        "active": True,
                        "planned_at": stamp(when),
                        "scheduled_at": stamp(effective),
                    },
                )
                desired.add(key)

    def plan_reminders(self, payload):
        subject = payload.get("subject")
        specified_user = payload.get("user_id")
        if specified_user:
            users = [specified_user]
        elif subject:
            users = sorted(
                {s["user_id"] for s in self.store.list("user_states") if s["event_id"] == subject["id"]}
                | {
                    p["user_id"]
                    for p in self.store.list("reminder_plans")
                    if p["subject"]["kind"] == subject["kind"] and p["subject"]["id"] == subject["id"]
                }
            )
        else:
            return
        now = self.clock()
        local_today = now.astimezone(TZ).date()
        for user_id in users:
            prefs = self.preferences(user_id)
            subjects = self.core.events.selected_subjects(user_id)
            if subject:
                subjects = [s for s in subjects if s["kind"] == subject["kind"] and s["id"] == subject["id"]] + [
                    subject
                ]
            if not subject:
                subjects += [e["subject"] for e in self.store.list("calendar_entries") if e["user_id"] == user_id]
                subjects += [p["subject"] for p in self.store.list("reminder_plans") if p["user_id"] == user_id]
            unique = {(s["kind"], s["id"]): s for s in subjects}
            desired = set()
            for s in unique.values():
                resolved = self.core.events.resolve_subject(user_id, s)
                if not resolved:
                    continue
                self._calendar(user_id, resolved, prefs)
                current = resolved["subject"]
                record = resolved["record"]
                cancelled = (
                    resolved["withdrawn"] or resolved["deleted"] or bool(record and record.get("status") == "cancelled")
                )
                selected = resolved["selected"]
                open_item = resolved["state"] not in ["done", "archived"]
                if prefs["reminders_enabled"] and selected and open_item and not cancelled:
                    for d in self._deadlines(resolved):
                        if d["precision"] == "unknown":
                            continue
                        cutoff = None
                        if d["precision"] == "minute":
                            cutoff = parse_time(d["at"])
                            if cutoff <= now:
                                continue
                            when = cutoff - timedelta(minutes=prefs["reminder_lead_minutes"])
                        else:
                            deadline_date = date.fromisoformat(d["date"])
                            if deadline_date < local_today:
                                continue
                            # Expiration is the next local day; this does not invent an official cutoff time.
                            cutoff = datetime.combine(
                                deadline_date + timedelta(days=1),
                                datetime.min.time(),
                                TZ,
                            ).astimezone(UTC)
                            rh, rm = map(int, prefs["date_only_reminder_time"].split(":"))
                            when = (
                                datetime.combine(
                                    deadline_date - timedelta(days=prefs["reminder_lead_minutes"] // 1440),
                                    datetime.min.time(),
                                    TZ,
                                )
                                .replace(hour=rh, minute=rm)
                                .astimezone(UTC)
                            )
                        when = max(now.replace(second=0, microsecond=0), when)
                        semantic = {k: d.get(k) for k in ["kind", "precision", "at", "date"]}
                        # Stable late-reminder time avoids duplicate plan keys on queue replay.
                        families = {
                            digest(
                                {
                                    "user_id": user_id,
                                    "kind": "deadline",
                                    "subject_kind": current["kind"],
                                    "subject_id": current["id"],
                                    "semantic": semantic,
                                    "channel": channel,
                                }
                            )
                            for channel in ["in_app", "email"]
                        }
                        previous = next(
                            (
                                p
                                for p in self.store.list("reminder_plans")
                                if p["user_id"] == user_id and p.get("family") in families
                            ),
                            None,
                        )
                        if previous:
                            planned = parse_time(previous.get("planned_at", previous["scheduled_at"]))
                            if planned <= now:
                                when = planned
                        self._plan(
                            user_id,
                            current,
                            "deadline",
                            semantic,
                            when,
                            prefs,
                            desired,
                            cutoff,
                        )
                if current["kind"] == "public_event" and selected and prefs["change_reminders_enabled"] and record:
                    state = self.core.events.state(user_id, current["id"]) or {}
                    important = (
                        self.core.events.changed_since(
                            current["id"],
                            state.get("acknowledged_version", current["revision"]),
                            record,
                        )
                        & self.core.events.IMPORTANT_FIELDS
                    )
                    if state.get("acknowledged_version", current["revision"]) < current["revision"] and important:
                        kind = (
                            "withdrawn"
                            if resolved["withdrawn"]
                            else "cancelled"
                            if record["status"] == "cancelled"
                            else "material_change"
                        )
                        changed_versions = [
                            version
                            for version in range(
                                state["acknowledged_version"] + 1,
                                current["revision"] + 1,
                            )
                            if set(
                                (
                                    record
                                    if version == current["revision"]
                                    else self.store.get("event_versions", f"{current['id']}:{version}") or {}
                                ).get("changed_fields", [])
                            )
                            & important
                        ]
                        semantic = {"version": max(changed_versions)}
                        when = max(
                            parse_time(record["updated_at"]),
                            now.replace(second=0, microsecond=0),
                        )
                        old = next(
                            (
                                p
                                for p in self.store.list("reminder_plans")
                                if p["user_id"] == user_id
                                and p["subject"]["kind"] == current["kind"]
                                and p["subject"]["id"] == current["id"]
                                and p["kind"] == kind
                                and p.get("semantic") == semantic
                            ),
                            None,
                        )
                        if old:
                            when = parse_time(old.get("planned_at", old["scheduled_at"]))
                        self._plan(user_id, current, kind, semantic, when, prefs, desired)
            for key, p in [
                (p["id"], p) for p in self.store.list("reminder_plans") if p["user_id"] == user_id and p["active"]
            ]:
                if subject and (p["subject"]["kind"], p["subject"]["id"]) != (
                    subject["kind"],
                    subject["id"],
                ):
                    continue
                if key not in desired:
                    with self.store.transaction():
                        p["active"] = False
                        self.store.put("reminder_plans", key, p)
                        d = self.store.get("deliveries", p["delivery_id"])
                        if d and d["status"] in ["scheduled", "failed"]:
                            d["status"] = "cancelled"
                            self.store.put("deliveries", d["id"], d)

    def build_digest(self, payload):
        user_id = payload["user_id"]
        local_date = self.clock().astimezone(TZ).date().isoformat()
        key = f"{user_id}:{local_date}"
        with self.store.transaction():
            prefs = self.preferences(user_id)
            if (
                not prefs["daily_digest_enabled"]
                or self.clock().astimezone(TZ).strftime("%H:%M") < prefs["digest_time"]
            ):
                return None
            existing = self.store.get("digests", key)
            if existing:
                return existing["digest"]
            cards = [
                c
                for c in self.core.events.feed(user_id)
                if (c["state"] or {}).get("state") not in ["done", "archived"] and c["event"]["status"] != "cancelled"
            ]
            private = [i for i in self.core.events.list_personal(user_id) if i["state"] not in ["done", "archived"]]
            item = {
                "id": uid(),
                "date": local_date,
                "cards": cards,
                "personal_items": private,
                "generated_at": stamp(self.clock()),
            }
            validate("Digest", item)
            self.store.put(
                "digests",
                key,
                {"user_id": user_id, "digest": item},
                expected_revision=0,
            )
            for channel in prefs["channels"]:
                delivery = {
                    "id": uid(),
                    "user_id": user_id,
                    "subject": None,
                    "channel": channel,
                    "kind": "digest",
                    "status": "scheduled",
                    "scheduled_at": stamp(self._quiet(self.clock(), prefs) if channel == "email" else self.clock()),
                    "sent_at": None,
                }
                self.store.put("deliveries", delivery["id"], delivery)
                self.store.put(
                    "delivery_contexts",
                    delivery["id"],
                    {"digest_id": item["id"], "digest_date": local_date},
                )
            return item

    def digests(self, user_id):
        current_cards = {
            c["event"]["id"]: c
            for c in self.core.events.feed(user_id)
            if (c["state"] or {}).get("state") not in ["done", "archived"] and c["event"]["status"] != "cancelled"
        }
        private = {
            i["id"]: i for i in self.core.events.list_personal(user_id) if i["state"] not in ["done", "archived"]
        }
        results = []
        for row in self.store.list("digests"):
            if row["user_id"] != user_id:
                continue
            item = row["digest"]
            item["cards"] = [
                current_cards[c["event"]["id"]] for c in item["cards"] if c["event"]["id"] in current_cards
            ]
            item["personal_items"] = [private[i["id"]] for i in item["personal_items"] if i["id"] in private]
            results.append(item)
        return sorted(results, key=lambda i: i["date"], reverse=True)

    def _message(self, user_id, delivery):
        if delivery["kind"] == "digest":
            context = self.store.get("delivery_contexts", delivery["id"])
            if not context:
                return None
            latest = next(
                (
                    item
                    for item in self.digests(user_id)
                    if item["id"] == context["digest_id"] and item["date"] == context["digest_date"]
                ),
                None,
            )
            if not latest:
                return None
            titles = [c["event"]["title"] for c in latest["cards"]] + [
                i["draft"]["title"] for i in latest["personal_items"]
            ]
            return "偷闲·天大版每日简报", "待处理事项：\n" + "\n".join(
                "- " + t for t in titles[:30]
            ) + "\n请打开工作台核对材料、期限与原文。"
        resolved = self.core.events.resolve_subject(user_id, delivery["subject"])
        if not resolved:
            return None
        if delivery["kind"] == "withdrawn":
            return "事项展示有更新", "你收藏的事项已撤回公开展示，请通过原渠道核对。"
        if resolved["withdrawn"] or resolved["deleted"]:
            return None
        record = resolved["record"]
        title = record["title"] if delivery["subject"]["kind"] == "public_event" else record["draft"]["title"]
        suffix = {
            "deadline": "请核对截止时间及办理要求",
            "material_change": "期限或办理要求有更新，请核对新版本",
            "cancelled": "事项已取消，请停止原计划",
        }[delivery["kind"]]
        return (
            title,
            title + "：" + suffix + "。日期只有天时，提醒时刻不是学校规定的具体截止时刻。",
        )

    def _deadline_cutoff(self, delivery, resolved):
        plan = next(
            (p for p in self.store.list("reminder_plans") if p["delivery_id"] == delivery["id"] and p["active"]),
            None,
        )
        semantic = (plan or {}).get("semantic")
        if not semantic:
            return None
        deadline = next(
            (
                d
                for d in self._deadlines(resolved)
                if {k: d.get(k) for k in ["kind", "precision", "at", "date"]} == semantic
            ),
            None,
        )
        if not deadline:
            return None
        if deadline["precision"] == "minute":
            return parse_time(deadline["at"])
        if deadline["precision"] == "date":
            return datetime.combine(
                date.fromisoformat(deadline["date"]) + timedelta(days=1),
                datetime.min.time(),
                TZ,
            ).astimezone(UTC)
        return None

    def deliver_message(self, payload):
        identifier = payload["delivery_id"]
        now = self.clock()
        with self.store.transaction():
            revision = self.store.revision("deliveries", identifier)
            d = self.store.get("deliveries", identifier)
            if (
                not d
                or d["user_id"] != payload["user_id"]
                or d["status"] in ["sending", "sent", "unknown", "cancelled"]
            ):
                return
            prefs = self.preferences(d["user_id"])
            invalid = d["channel"] not in prefs["channels"] or (
                d["kind"] == "digest" and not prefs["daily_digest_enabled"]
            )
            cutoff = None
            if d["kind"] == "digest":
                context = self.store.get("delivery_contexts", identifier)
                invalid = invalid or not context or context["digest_date"] != now.astimezone(TZ).date().isoformat()
            if d["subject"]:
                resolved = self.core.events.resolve_subject(d["user_id"], d["subject"])
                invalid = (
                    invalid
                    or not resolved
                    or resolved["subject"]["revision"] != d["subject"]["revision"]
                    or not resolved["selected"]
                )
                if resolved and d["kind"] == "deadline":
                    cutoff = self._deadline_cutoff(d, resolved)
                    invalid = (
                        invalid
                        or cutoff is None
                        or cutoff <= now
                        or resolved["state"] in ["done", "archived"]
                        or resolved["withdrawn"]
                        or resolved["deleted"]
                        or (resolved["record"] or {}).get("status") == "cancelled"
                        or not prefs["reminders_enabled"]
                    )
                elif d["kind"] != "deadline":
                    invalid = invalid or not prefs["change_reminders_enabled"]
                    if resolved and d["subject"]["kind"] == "public_event":
                        state = self.core.events.state(d["user_id"], d["subject"]["id"]) or {}
                        invalid = (
                            invalid
                            or state.get("acknowledged_version", resolved["subject"]["revision"])
                            >= resolved["subject"]["revision"]
                        )
            if invalid:
                d["status"] = "cancelled"
                self.store.put("deliveries", identifier, d, expected_revision=revision)
                return
            if parse_time(d["scheduled_at"]) > now:
                return
            if d["channel"] in QUIET_CHANNELS and self._quiet(now, prefs) > now:
                effective = self._quiet(now, prefs)
                if cutoff and effective >= cutoff:
                    d["status"] = "cancelled"
                else:
                    d["scheduled_at"] = stamp(effective)
                self.store.put("deliveries", identifier, d, expected_revision=revision)
                return
            message = self._message(d["user_id"], d)
            if message is None:
                d["status"] = "cancelled"
                self.store.put("deliveries", identifier, d, expected_revision=revision)
                return
            claim = uid()
            d["status"] = "sending"
            self.store.put("deliveries", identifier, d, expected_revision=revision)
            self.store.put(
                "delivery_claims",
                identifier,
                {"claim": claim, "claimed_at": stamp(now)},
            )
        if d["channel"] == "in_app":
            receipt = {"status": "sent"}
        elif d["channel"] == "email":
            user = self.store.get("users", d["user_id"])
            receipt = (
                self.core.mail.send_delivery(user["email"], message[0], message[1], identifier)
                if user
                else {"status": "failed"}
            )
        else:
            # A private message can only reach destinations bound to its owner.
            receipt = self.core.push.send_delivery_for_user(d["user_id"], message[0], message[1], identifier)
        with self.store.transaction():
            current = self.store.get("deliveries", identifier)
            if (self.store.get("delivery_claims", identifier) or {}).get("claim") != claim or current[
                "status"
            ] != "sending":
                return
            current["status"] = receipt["status"]
            current["sent_at"] = stamp(now) if current["status"] == "sent" else None
            self.store.put("deliveries", identifier, current)
        if receipt["status"] == "failed":
            raise APIError(
                503,
                "delivery_failed",
                "投递未被接受，请检查个人渠道绑定或邮件配置后重试",
            )

    def recover_abandoned_sends(self):
        # SMTP's configured maximum is 120 seconds; no resend after an uncertain crash.
        cutoff = self.clock() - timedelta(seconds=180)
        for row in self.store.list("deliveries"):
            if row["status"] != "sending":
                continue
            with self.store.transaction():
                rev = self.store.revision("deliveries", row["id"])
                current = self.store.get("deliveries", row["id"])
                claim = self.store.get("delivery_claims", row["id"]) or {}
                if (
                    current["status"] == "sending"
                    and claim.get("claimed_at")
                    and parse_time(claim["claimed_at"]) < cutoff
                ):
                    current["status"] = "unknown"
                    self.store.put("deliveries", row["id"], current, expected_revision=rev)

    def token(self, user_id, request):
        key = request.headers.get("Idempotency-Key", "")
        if not key or len(key) > 200:
            raise APIError(400, "idempotency_required", "请提供请求标识")
        identity = hashlib.sha256((user_id + ":calendar:" + key).encode()).hexdigest()
        with self.store.transaction():
            previous = self.store.get("idempotency", identity)
            if previous:
                salt, issued = previous["salt"], previous["issued_at"]
            else:
                salt, issued = uid(), now_iso()
                self.store.put(
                    "idempotency",
                    identity,
                    {
                        "user_id": user_id,
                        "operation": "calendar",
                        "salt": salt,
                        "issued_at": issued,
                    },
                )
            raw = hmac.new(
                self.core.settings.secret.encode(),
                (user_id + ":" + salt).encode(),
                hashlib.sha256,
            ).hexdigest()
            if not previous:
                self.store.put(
                    "calendar_tokens",
                    user_id,
                    {
                        "user_id": user_id,
                        "token_hash": hashlib.sha256(raw.encode()).hexdigest(),
                        "issued_at": issued,
                    },
                )
            return {"token": raw, "issued_at": issued}

    def revoke_token(self, user_id):
        self.store.delete("calendar_tokens", user_id)

    @staticmethod
    def _escape(text):
        return (
            str(text)
            .replace("\\", "\\\\")
            .replace("\r", "")
            .replace("\n", "\\n")
            .replace(";", "\\;")
            .replace(",", "\\,")[:4000]
        )

    @staticmethod
    def _fold(line):
        parts = []
        chunk = ""
        for char in line:
            if len((chunk + char).encode()) > 74:
                parts.append(chunk)
                chunk = " " + char
            else:
                chunk += char
        parts.append(chunk)
        return "\r\n".join(parts)

    def calendar(self, raw_token):
        wanted = hashlib.sha256(raw_token.encode()).hexdigest()
        token = next(
            (t for t in self.store.list("calendar_tokens") if hmac.compare_digest(t["token_hash"], wanted)),
            None,
        )
        if not token:
            raise APIError(404, "token_revoked", "日历订阅不存在或已经撤销")
        user_id = token["user_id"]
        self.plan_reminders({"user_id": user_id, "subject": None})
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Touxian//TJU Campus//ZH",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "X-WR-CALNAME:偷闲·天大版",
        ]
        for entry in self.store.list("calendar_entries"):
            if entry["user_id"] != user_id or not (entry["start_date"] or entry["start_at"]):
                continue
            lines += [
                "BEGIN:VEVENT",
                "UID:" + entry["uid"],
                "SEQUENCE:" + str(entry["sequence"]),
                "DTSTAMP:" + parse_time(entry["dtstamp"]).strftime("%Y%m%dT%H%M%SZ"),
            ]
            if entry["start_date"]:
                start = date.fromisoformat(entry["start_date"])
                lines += [
                    "DTSTART;VALUE=DATE:" + start.strftime("%Y%m%d"),
                    "DTEND;VALUE=DATE:" + (start + timedelta(days=1)).strftime("%Y%m%d"),
                ]
            else:
                lines += ["DTSTART:" + parse_time(entry["start_at"]).strftime("%Y%m%dT%H%M%SZ")]
            lines += [
                "STATUS:" + entry["status"],
                "SUMMARY:" + self._escape(entry["title"]),
                "DESCRIPTION:" + self._escape(entry["description"]),
                "END:VEVENT",
            ]
        lines += ["END:VCALENDAR"]
        return "\r\n".join(self._fold(line) for line in lines) + "\r\n"


def make_router(core):
    router = APIRouter()

    @router.get("/me/preferences")
    def preferences(request: Request):
        return core.delivery.preferences(require_user(request)["id"])

    @router.put("/me/preferences")
    def set_preferences(request: Request, body: dict = Body(...)):
        return core.delivery.update_preferences(require_user(request, write=True)["id"], body)

    @router.get("/me/push-status")
    def push_status(request: Request):
        return core.push.status(require_user(request)["id"])

    @router.get("/me/digests")
    def digests(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ):
        return page(core.delivery.digests(require_user(request)["id"]), cursor, limit)

    @router.get("/me/deliveries")
    def deliveries(
        request: Request,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ):
        user = require_user(request)
        values = [d for d in core.store.list("deliveries") if d["user_id"] == user["id"]]
        return page(sorted(values, key=lambda d: d["scheduled_at"], reverse=True), cursor, limit)

    @router.post("/me/calendar-token")
    def token(request: Request):
        return core.delivery.token(require_user(request, write=True)["id"], request)

    @router.delete("/me/calendar-token", status_code=204)
    def revoke(request: Request):
        core.delivery.revoke_token(require_user(request, write=True)["id"])

    @router.get("/calendar/{token}.ics")
    def calendar(token: str):
        return Response(
            core.delivery.calendar(token),
            media_type="text/calendar",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="tju-calendar.ics"',
            },
        )

    return router
