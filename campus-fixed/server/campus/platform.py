"""Accounts, settings, scoped permissions and shared transport for the campus app."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .schema import now_iso, uid, validate

COOKIE_NAME = "touxian_session"
PRODUCTION_ENVS = {"production", "prod"}
COLLEGE_SOURCE = "https://www.tju.edu.cn/xy_b_.htm"
# Stable application IDs; names checked against the official list on 2026-10-08.
COLLEGES = [
    ("mechanical", "机械工程学院"),
    ("precision", "精密仪器与光电子工程学院"),
    ("electrical", "电气自动化与信息工程学院"),
    ("microelectronics", "微电子学院"),
    ("computer", "智能与计算学部-计算机科学与技术学院"),
    ("cybersecurity", "智能与计算学部-网络安全学院"),
    ("artificial-intelligence", "智能与计算学部-人工智能学院"),
    ("software", "智能与计算学部-软件学院"),
    ("civil", "建筑工程学院"),
    ("architecture", "建筑学院"),
    ("chemical", "化工学院"),
    ("synthetic-biology", "合成生物与生物制造学院"),
    ("materials", "材料科学与工程学院"),
    ("environment", "环境科学与工程学院"),
    ("management", "管理与经济学部"),
    ("humanities-arts", "人文艺术学院"),
    ("foreign-languages", "外国语学院"),
    ("law", "法学院、纪检监察学院"),
    ("marxism", "马克思主义学院"),
    ("science", "理学院"),
    ("mathematics", "数学学院"),
    ("medicine", "医学部-医学院"),
    ("pharmaceutical", "医学部-药物科学与技术学院"),
    ("life-sciences", "医学部-生命科学学院"),
    ("health-emergency", "医学部-卫生应急学院"),
    ("marine", "海洋科学与技术学院"),
    ("earth", "地球系统科学学院"),
    ("energy", "能源学院"),
    ("future-technology", "未来技术学院"),
    ("xuanhuai", "宣怀学院"),
    ("excellent-engineers", "国家卓越工程师学院、国际工程师学院"),
    ("education", "教育学院"),
    ("international-education", "国际教育学院"),
    ("new-media", "新媒体与传播学院"),
    ("molecule-plus", "分子+研究院"),
    ("molecular-aggregation", "分子聚集态科学研究院"),
]


def _true(value):
    return str(value).strip().lower() in {"1", "true", "yes"}


def _moment(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _later(seconds):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status = status
        self.status_code = status
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class Settings:
    env: str = "development"
    database_url: str = ""
    data_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parents[2] / ".campus-data")
    secret: str = ""
    admin_emails: set[str] = field(default_factory=set)
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            f"http://{host}:{port}" for port in (5173, 4173, 8000) for host in ("localhost", "127.0.0.1")
        ]
    )
    dev_auth: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_timeout: float = 15.0
    session_seconds: int = 7 * 24 * 3600

    def __post_init__(self):
        self.env = self.env.strip().lower()
        if self.env not in {"development", "dev", "test", "testing", "production", "prod"}:
            raise ValueError("CAMPUS_ENV must be development, test, or production")
        self.data_dir = Path(self.data_dir).resolve()
        self.admin_emails = {email.strip().lower() for email in self.admin_emails if email.strip()}
        self.allowed_origins = [origin.rstrip("/") for origin in self.allowed_origins]
        if "*" in self.allowed_origins:
            raise ValueError("Credentialed sessions require explicit allowed origins")
        if not self.database_url:
            self.database_url = "sqlite:///" + (self.data_dir / "campus.sqlite3").as_posix()
        if self.production:
            if self.dev_auth:
                raise ValueError("CAMPUS_DEV_AUTH must be false in production")
            if len(self.secret) < 32:
                raise ValueError("CAMPUS_SECRET must contain at least 32 characters in production")
            if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("Production requires PostgreSQL")
            if not self.allowed_origins or any(not value.startswith("https://") for value in self.allowed_origins):
                raise ValueError("Production origins must be explicitly configured HTTPS URLs")
            if self.smtp_host and not (self.smtp_starttls or self.smtp_ssl):
                raise ValueError("Production SMTP requires TLS")
        if not 1 <= self.smtp_port <= 65535 or not 1 <= self.smtp_timeout <= 120:
            raise ValueError("Invalid SMTP port/timeout")

    @property
    def production(self):
        return self.env in PRODUCTION_ENVS

    @classmethod
    def from_env(cls):
        return cls(
            env=os.getenv("CAMPUS_ENV", "development"),
            database_url=os.getenv("CAMPUS_DATABASE_URL", ""),
            data_dir=Path(os.getenv("CAMPUS_DATA_DIR", str(Path(__file__).resolve().parents[2] / ".campus-data"))),
            secret=os.getenv("CAMPUS_SECRET", ""),
            admin_emails=set(os.getenv("CAMPUS_ADMIN_EMAILS", "").split(",")),
            allowed_origins=[
                s.strip()
                for s in os.getenv(
                    "CAMPUS_ALLOWED_ORIGINS",
                    ",".join(
                        f"http://{host}:{port}" for port in (5173, 4173, 8000) for host in ("localhost", "127.0.0.1")
                    ),
                ).split(",")
                if s.strip()
            ],
            dev_auth=_true(os.getenv("CAMPUS_DEV_AUTH", "false")),
            smtp_host=os.getenv("CAMPUS_SMTP_HOST", ""),
            smtp_port=int(os.getenv("CAMPUS_SMTP_PORT", "587")),
            smtp_username=os.getenv("CAMPUS_SMTP_USERNAME", ""),
            smtp_password=os.getenv("CAMPUS_SMTP_PASSWORD", ""),
            smtp_from=os.getenv("CAMPUS_SMTP_FROM", ""),
            smtp_starttls=_true(os.getenv("CAMPUS_SMTP_STARTTLS", "true")),
            smtp_ssl=_true(os.getenv("CAMPUS_SMTP_SSL", "false")),
            smtp_timeout=float(os.getenv("CAMPUS_SMTP_TIMEOUT", "15")),
        )


class MailGateway:
    """SMTP acceptance is not proof of inbox delivery; timeouts may be ambiguous."""

    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self):
        return bool(self.settings.smtp_host and self.settings.smtp_from)

    def send_login_code(self, email, code):
        return self.send_delivery(
            email, "偷闲·天大版登录验证码", f"你的登录验证码是 {code}，10分钟内有效。未发起登录可忽略此邮件。", uid()
        )

    def send_delivery(self, email, subject, text, idempotency_key):
        if not self.settings.production or not self.configured:
            return {"status": "failed", "provider_id": None}
        message = EmailMessage()
        try:
            message["From"] = self.settings.smtp_from
            message["To"] = email
            message["Subject"] = subject
            receipt_id = hashlib.sha256(str(idempotency_key).encode()).hexdigest()
            message["Message-ID"] = f"<{receipt_id}@touxian.local>"
            message.set_content(text)
        except (ValueError, TypeError):
            return {"status": "failed", "provider_id": None}
        sending = False
        try:
            factory = smtplib.SMTP_SSL if self.settings.smtp_ssl else smtplib.SMTP
            kwargs = {"timeout": self.settings.smtp_timeout}
            if self.settings.smtp_ssl:
                kwargs["context"] = ssl.create_default_context()
            with factory(self.settings.smtp_host, self.settings.smtp_port, **kwargs) as smtp:
                smtp.ehlo()
                if self.settings.smtp_starttls and not self.settings.smtp_ssl:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if self.settings.smtp_username:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                sending = True
                refused = smtp.send_message(message)
                if refused:
                    return {"status": "failed", "provider_id": None}
                return {"status": "sent", "provider_id": receipt_id}
        except (
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPDataError,
        ):
            return {"status": "failed", "provider_id": None}
        except (TimeoutError, OSError, smtplib.SMTPException):
            return {"status": "unknown" if sending else "failed", "provider_id": None}


class Core:
    def __init__(self, settings):
        from .db import Store

        self.settings = settings
        self.data_dir = settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not settings.secret:
            secret_path = self.data_dir / ".server-secret"
            temporary = self.data_dir / (".secret-" + secrets.token_hex(16))
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(secrets.token_urlsafe(48))
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # The shared name appears only after all bytes have been written.
                    os.link(temporary, secret_path)
                except FileExistsError:
                    pass
                settings.secret = secret_path.read_text(encoding="ascii").strip()
            finally:
                temporary.unlink(missing_ok=True)
            if len(settings.secret) < 32:
                raise RuntimeError("Invalid persisted development secret")
        self.store = Store(settings.database_url)
        self.mail = MailGateway(settings)
        # Private destinations are bound to individual users. Global broadcast
        # configuration can never receive reminders or personal digest content.
        from .channels import PushGateway

        self.push = PushGateway()
        self.handlers = {}
        catalog = institution_catalog()
        with self.store.transaction():
            existing = self.store.get("catalog", "tju")
            if existing is None or existing.get("revision") != catalog["revision"]:
                self.store.put("catalog", "tju", catalog)
        # No student data, sample notices, sources or admin accounts are implicitly seeded.

    def digest(self, value):
        return hmac.new(self.settings.secret.encode(), str(value).encode(), hashlib.sha256).hexdigest()


def institution_catalog():
    result = {
        "school_name": "天津大学",
        "revision": "tju-official-2026-10-08",
        "colleges": [{"id": ident, "name": name} for ident, name in COLLEGES],
        "education_levels": [
            {"id": ident, "name": name}
            for ident, name in [
                ("undergraduate", "本科生"),
                ("master", "硕士研究生"),
                ("doctor", "博士研究生"),
                ("other", "其他"),
                ("unknown", "未设置"),
            ]
        ],
        "campuses": [
            {"id": ident, "name": name}
            for ident, name in [
                ("weijinlu", "卫津路校区"),
                ("beiyangyuan", "北洋园校区"),
                ("other", "其他"),
                ("unknown", "未设置"),
            ]
        ],
    }
    validate("InstitutionCatalog", result)
    return result


def _origin_check(request, core):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in core.settings.allowed_origins:
        raise APIError(403, "ORIGIN_FORBIDDEN", "此来源不允许修改账号数据")


def require_user(request, roles=None, write=False):
    core = request.app.state.core
    token = request.cookies.get(COOKIE_NAME)
    if not token or len(token) > 256:
        raise APIError(401, "UNAUTHENTICATED", "请先登录")
    session = core.store.get("sessions", core.digest(token))
    if not session or _moment(session["expires_at"]) <= datetime.now(UTC):
        raise APIError(401, "SESSION_EXPIRED", "登录已过期，请重新登录")
    user = core.store.get("users", session["user_id"])
    if not user:
        raise APIError(401, "SESSION_EXPIRED", "登录已过期，请重新登录")
    if user["role"] == "admin" and user["email"] not in core.settings.admin_emails:
        user = {**user, "role": "student"}
    if roles is not None and user["role"] not in roles:
        raise APIError(403, "FORBIDDEN", "当前账号无此操作权限")
    if write:
        _origin_check(request, core)
        token = request.headers.get("X-CSRF-Token", "")
        if not token or not hmac.compare_digest(token, session["csrf_token"]):
            raise APIError(403, "CSRF_INVALID", "会话校验失败，请刷新后重试")
    request.state.session = session
    request.state.user = user
    return user


def error_handlers(app: FastAPI):
    @app.middleware("http")
    async def request_context(request, call_next):
        request.state.request_id = uid()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    def error_response(request, status, code, message, details=None):
        request_id = getattr(request.state, "request_id", uid())
        return JSONResponse(
            status_code=status,
            content={
                "code": code,
                "message": message,
                "request_id": request_id,
                "details": details or {},
            },
            headers={"X-Request-ID": request_id, "Cache-Control": "no-store"},
        )

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return error_response(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error_response(
            request,
            422,
            "VALIDATION_ERROR",
            "请求参数格式错误",
            {"errors": [{"path": "/".join(map(str, e["loc"])), "rule": e["type"]} for e in exc.errors()[:12]]},
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, exc.status_code, f"HTTP_{exc.status_code}", "请求无法处理")

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        return error_response(request, 500, "INTERNAL_ERROR", "服务暂时无法处理请求")


async def _body(request, schema):
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > 65536:
            raise APIError(413, "BODY_TOO_LARGE", "请求数据过大")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise APIError(400, "INVALID_JSON", "请求必须为有效JSON") from None
    validate(schema, data)
    return data


def _if_match(request):
    value = request.headers.get("if-match")
    if value is None:
        return None
    value = value.strip('"')
    if not value.isdigit():
        raise APIError(400, "REVISION_INVALID", "If-Match应为非负版本号")
    return int(value)


def _etag(core, response, collection, key):
    response.headers["ETag"] = f'"{core.store.revision(collection, key)}"'
    response.headers["Cache-Control"] = "no-store"


def _uuid(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError):
        raise APIError(422, "VALIDATION_ERROR", "标识格式错误") from None


def make_router(core):
    router = APIRouter()

    @router.post("/auth/challenges", status_code=201)
    async def challenge(request: Request, response: Response):
        _origin_check(request, core)
        data = await _body(request, "LoginChallengeRequest")
        email = data["email"].strip().lower()
        if len(email) > 254:
            raise APIError(422, "EMAIL_INVALID", "邮箱地址过长")
        ip_hash = core.digest(request.client.host if request.client else "unknown")
        email_hash = core.digest(email)
        now = datetime.now(UTC)
        with core.store.transaction():
            for scope in ("challenge-ip:" + ip_hash, "challenge-rate:" + email_hash):
                gate = core.store.revision("idempotency", scope)
                core.store.put("idempotency", scope, {"at": now_iso()}, expected_revision=gate)
            recent = [c for c in core.store.list("challenges") if _moment(c["created_at"]) > now - timedelta(hours=1)]
            email_recent = [c for c in recent if c["email_hash"] == email_hash]
            ip_recent = [c for c in recent if c["ip_hash"] == ip_hash]
            if (
                len(email_recent) >= 6
                or len(ip_recent) >= 30
                or any(_moment(c["created_at"]) > now - timedelta(seconds=30) for c in email_recent)
            ):
                raise APIError(429, "RATE_LIMITED", "验证码请求过于频繁，请稍后重试")
            challenge_id = uid()
            code = f"{secrets.randbelow(1000000):06d}"
            expires = _later(600)
            record = {
                "id": challenge_id,
                "email": email,
                "email_hash": email_hash,
                "ip_hash": ip_hash,
                "code_hash": core.digest(challenge_id + ":" + code),
                "created_at": now_iso(),
                "expires_at": expires,
                "attempts": 0,
                "used": False,
                "delivery_status": "pending",
            }
            core.store.put("challenges", challenge_id, record, expected_revision=0)
        if core.settings.dev_auth and not core.settings.production:
            receipt = {"status": "development", "provider_id": None}
            response.headers["X-Dev-Login-Code"] = code
        else:
            receipt = core.mail.send_login_code(email, code)
        with core.store.transaction():
            record = core.store.get("challenges", challenge_id)
            record["delivery_status"] = receipt["status"]
            core.store.put("challenges", challenge_id, record)
        if receipt["status"] in {"failed", "unknown"}:
            raise APIError(
                503,
                "LOGIN_DELIVERY_UNAVAILABLE",
                "登录邮件未确认发送；请检查邮件服务配置"
                if receipt["status"] == "failed"
                else "登录邮件发送结果未知，请稍后查看邮箱",
                {"delivery_status": receipt["status"]},
            )
        response.headers["Cache-Control"] = "no-store"
        return {"challenge_id": challenge_id, "expires_at": expires}

    @router.post("/auth/sessions")
    async def login(request: Request, response: Response):
        _origin_check(request, core)
        data = await _body(request, "LoginVerifyRequest")
        failure = False
        with core.store.transaction():
            current = core.store.get("challenges", data["challenge_id"])
            revision = core.store.revision("challenges", data["challenge_id"])
            if (
                not current
                or current["used"]
                or current["attempts"] >= 5
                or (_moment(current["expires_at"]) <= datetime.now(UTC))
            ):
                failure = True
            else:
                current["attempts"] += 1
                if not hmac.compare_digest(
                    current["code_hash"], core.digest(data["challenge_id"] + ":" + data["code"])
                ):
                    failure = True
                else:
                    current["used"] = True
                core.store.put("challenges", data["challenge_id"], current, expected_revision=revision)
            if not failure:
                email = current["email"]
                identity_key = "user-email:" + core.digest(email)
                identity = core.store.get("idempotency", identity_key)
                user = core.store.get("users", identity["user_id"]) if identity else None
                if user is None:
                    user = {
                        "id": uid(),
                        "email": email,
                        "role": "admin" if email in core.settings.admin_emails else "student",
                    }
                    validate("User", user)
                    core.store.put("users", user["id"], user, expected_revision=0)
                    core.store.put("idempotency", identity_key, {"user_id": user["id"]}, expected_revision=0)
                    core.store.put(
                        "profiles",
                        user["id"],
                        {
                            "college_id": None,
                            "major": None,
                            "entry_year": None,
                            "education_level": "unknown",
                            "campus": "unknown",
                            "interests": [],
                        },
                    )
                    core.store.put("subscriptions", user["id"], {"source_ids": [], "event_types": []})
                else:
                    role = (
                        "admin"
                        if email in core.settings.admin_emails
                        else ("operator" if user["role"] == "operator" else "student")
                    )
                    if user["role"] != role:
                        user = {**user, "role": role}
                        core.store.put("users", user["id"], user)
                token = secrets.token_urlsafe(48)
                csrf = secrets.token_urlsafe(32)
                session = {
                    "user_id": user["id"],
                    "csrf_token": csrf,
                    "created_at": now_iso(),
                    "expires_at": _later(core.settings.session_seconds),
                }
                core.store.put("sessions", core.digest(token), session, expected_revision=0)
        if failure:
            raise APIError(401, "LOGIN_CODE_INVALID", "验证码无效或已过期")
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=core.settings.session_seconds,
            httponly=True,
            secure=core.settings.production,
            samesite="lax",
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        result = {"user": user, "csrf_token": csrf}
        validate("SessionResponse", result)
        return result

    @router.delete("/auth/session", status_code=204)
    def logout(request: Request):
        require_user(request, write=True)
        core.store.delete("sessions", core.digest(request.cookies[COOKIE_NAME]))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/", secure=core.settings.production, httponly=True, samesite="lax")
        return response

    @router.get("/me")
    def me(request: Request, response: Response):
        user = require_user(request)
        response.headers["Cache-Control"] = "no-store"
        return {"user": user, "csrf_token": request.state.session["csrf_token"]}

    @router.get("/catalog")
    def catalog():
        return core.store.get("catalog", "tju")

    @router.get("/me/profile")
    def profile(request: Request, response: Response):
        user = require_user(request)
        _etag(core, response, "profiles", user["id"])
        return core.store.get("profiles", user["id"])

    @router.put("/me/profile")
    async def update_profile(request: Request, response: Response):
        user = require_user(request, write=True)
        data = await _body(request, "Profile")
        if data["college_id"] is not None and data["college_id"] not in {c[0] for c in COLLEGES}:
            raise APIError(422, "COLLEGE_UNKNOWN", "请选择学院目录中的学院")
        if len(data["interests"]) > 100 or any(len(v) > 100 for v in data["interests"]):
            raise APIError(422, "PROFILE_TOO_LARGE", "关注项过多或过长")
        if data["major"] is not None and len(data["major"]) > 200:
            raise APIError(422, "PROFILE_TOO_LARGE", "专业名称过长")
        with core.store.transaction():
            core.store.put("profiles", user["id"], data, expected_revision=_if_match(request))
            revision = core.store.revision("profiles", user["id"])
            core.store.emit("profile.changed", user["id"], revision, user_id=user["id"])
        _etag(core, response, "profiles", user["id"])
        return data

    @router.get("/me/subscriptions")
    def subscriptions(request: Request, response: Response):
        user = require_user(request)
        _etag(core, response, "subscriptions", user["id"])
        return core.store.get("subscriptions", user["id"])

    @router.put("/me/subscriptions")
    async def update_subscriptions(request: Request, response: Response):
        user = require_user(request, write=True)
        data = await _body(request, "Subscriptions")
        if len(data["source_ids"]) > 200 or len(data["event_types"]) > 100:
            raise APIError(422, "SUBSCRIPTIONS_TOO_LARGE", "订阅项过多")
        if any(not core.store.get("sources", source_id) for source_id in data["source_ids"]):
            raise APIError(422, "SOURCE_UNKNOWN", "订阅源不存在，请刷新列表")
        data = {key: list(dict.fromkeys(value)) if isinstance(value, list) else value for key, value in data.items()}
        with core.store.transaction():
            core.store.put("subscriptions", user["id"], data, expected_revision=_if_match(request))
            revision = core.store.revision("subscriptions", user["id"])
            core.store.emit("subscriptions.changed", user["id"], revision, user_id=user["id"])
        _etag(core, response, "subscriptions", user["id"])
        return data

    @router.get("/admin/jobs")
    def jobs(request: Request, cursor: str | None = None, limit: int = 20):
        require_user(request, {"admin", "operator"})
        if not 1 <= limit <= 100:
            raise APIError(422, "LIMIT_INVALID", "每页数量应为1到100")
        items = core.store.jobs()
        start = 0
        if cursor:
            ids = [job["id"] for job in items]
            if cursor not in ids:
                raise APIError(400, "CURSOR_INVALID", "分页标识无效")
            start = ids.index(cursor) + 1
        page = items[start : start + limit]
        return {"items": page, "next_cursor": page[-1]["id"] if page and start + limit < len(items) else None}

    @router.get("/admin/jobs/{job_id}")
    def get_job(request: Request, job_id: str):
        require_user(request, {"admin", "operator"})
        job = core.store.get_job(_uuid(job_id))
        if not job:
            raise APIError(404, "JOB_NOT_FOUND", "任务不存在")
        return job

    @router.post("/admin/jobs/{job_id}/retry", status_code=202)
    def retry(request: Request, job_id: str):
        user = require_user(request, {"admin", "operator"}, write=True)
        job_id = _uuid(job_id)
        key = request.headers.get("Idempotency-Key", "")
        if not 1 <= len(key) <= 200:
            raise APIError(422, "IDEMPOTENCY_KEY_INVALID", "请提供1到200字符的幂等键")
        identity = "retry:" + user["id"] + ":" + core.digest(key)
        with core.store.transaction():
            existing = core.store.get("idempotency", identity)
            if existing:
                if existing["job_id"] != job_id:
                    raise APIError(409, "IDEMPOTENCY_CONFLICT", "同一幂等键不能重放不同任务")
                return existing["response"]
            job = core.store.retry_job(job_id)
            core.store.put("idempotency", identity, {"job_id": job_id, "response": job}, expected_revision=0)
            return job

    @router.get("/health/live")
    def live():
        return {"status": "alive"}

    @router.get("/health/ready")
    def ready():
        try:
            core.store.revision("catalog", "tju")
        except Exception:
            raise APIError(503, "NOT_READY", "数据库暂未就绪") from None
        return {
            "status": "ready",
            "demo_data": bool(core.store.get("runtime", "demo")) or _true(os.getenv("CAMPUS_DEMO", "false")),
            "public_sample": (core.data_dir / "PUBLIC_SAMPLE.json").is_file(),
            "login_delivery": "development"
            if core.settings.dev_auth
            else ("smtp_configured" if core.mail.configured and core.settings.production else "unavailable"),
        }

    return router
