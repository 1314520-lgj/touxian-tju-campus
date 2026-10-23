"""Bounded operator-provided attachment replacements; no fetches or auto-publication."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from .platform import APIError
from .schema import now_iso, uid, validate

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_XML_DEPTH = 32
MAX_XML_NODES = 100_000
MAX_EXTRACTED_TEXT = 500_000
MIMES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
}
PROVENANCE = "运营人员依据授权提供，未由系统从原网页获取"


class _BoundedOfficeXML:
    """Stop while building XML, including text the downstream parser repeats."""

    def __init__(self, budget):
        self.builder = ET.TreeBuilder()
        self.budget = budget
        self.tags = []
        self.paragraphs = 0

    def _text(self, count):
        self.budget["text"] += count
        if self.budget["text"] > MAX_EXTRACTED_TEXT:
            raise ValueError("office_text_limit")

    def start(self, tag, attributes):
        self.budget["nodes"] += 1
        if len(self.tags) >= MAX_XML_DEPTH or self.budget["nodes"] > MAX_XML_NODES:
            raise ValueError("office_structure_limit")
        local = tag.rsplit("}", 1)[-1]
        self.tags.append(local)
        if local in {"p", "row"}:
            self.paragraphs += 1
        # Each t/v contributes to every ancestor p/row, plus a join separator.
        # Text boxes may legitimately nest p elements; account for that work.
        if local in {"t", "v"}:
            self._text(self.paragraphs)
        return self.builder.start(tag, attributes)

    def end(self, tag):
        result = self.builder.end(tag)
        if self.tags.pop() in {"p", "row"}:
            self.paragraphs -= 1
        return result

    def data(self, text):
        if self.tags and self.tags[-1] in {"t", "v"}:
            self._text(len(text) * self.paragraphs)
        self.builder.data(text)

    def doctype(self, name, pubid, system):
        # A parser callback also catches declarations in UTF-16 XML.
        raise ValueError("unsafe_xml")

    def close(self):
        return self.builder.close()


def _office_xml(raw, budget):
    parser = ET.XMLParser(target=_BoundedOfficeXML(budget))
    parser.feed(raw)
    return parser.close()


def validate_file(body, filename, content_type):
    """Check real file structure, not just a client filename or MIME declaration."""
    if not body:
        raise APIError(422, "empty_attachment", "请选择非空文件")
    if len(body) > MAX_UPLOAD_BYTES:
        raise APIError(413, "attachment_too_large", "附件不能超过 8 MiB")
    extension = Path(filename or "").suffix.lower().lstrip(".")
    if extension not in MIMES:
        raise APIError(422, "unsupported_attachment_type", "仅支持 PDF、DOCX、XLSX 或 UTF-8 TXT 文件")
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    allowed = {MIMES[extension], "application/octet-stream", ""}
    if extension in {"docx", "xlsx"}:
        allowed.add("application/zip")
    if mime not in allowed:
        raise APIError(422, "attachment_type_mismatch", "文件内容、文件名和格式声明不一致")
    try:
        if extension == "txt":
            text = body.decode("utf-8-sig")
            if not text.strip() or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
                raise ValueError()
            if re.match(r"\s*<(?:!doctype\s+html|html|head|body|script|form)\b", text, re.I):
                raise ValueError()
            if body.startswith((b"%PDF-", b"PK\x03\x04")):
                raise ValueError()
            return "text", MIMES[extension]
        if extension == "pdf":
            if not body.startswith(b"%PDF-"):
                raise ValueError()
            # The same isolation used by the parser bounds complex PDF structures.
            script = """
import io,sys
try:
 import resource
 resource.setrlimit(resource.RLIMIT_AS,(768*1024*1024,768*1024*1024))
except ImportError:
 pass
from pypdf import PdfReader
r=PdfReader(io.BytesIO(sys.stdin.buffer.read()),strict=False)
if r.is_encrypted or not 1<=len(r.pages)<=100: raise ValueError('unsupported_pdf')
"""
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            result = subprocess.run(
                [sys.executable, "-c", script], input=body, capture_output=True, timeout=15, creationflags=flags
            )
            if result.returncode:
                raise ValueError()
        else:
            if not body.startswith(b"PK\x03\x04"):
                raise ValueError()
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                entries = archive.infolist()
                if len(entries) > 1000 or sum(e.file_size for e in entries) > 32 * 1024 * 1024:
                    raise ValueError()
                if any(
                    e.flag_bits & 1
                    or e.file_size > 32 * 1024 * 1024
                    or (e.compress_size and e.file_size / e.compress_size > 200)
                    for e in entries
                ):
                    raise ValueError()
                names = {e.filename for e in entries}
                # OPC parts are unique. The parser reads worksheet names again,
                # so duplicate ZIP names could repeat the last part many times.
                if len(names) != len(entries):
                    raise ValueError("duplicate_office_part")
                required = "word/document.xml" if extension == "docx" else "xl/workbook.xml"
                opposite = "xl/workbook.xml" if extension == "docx" else "word/document.xml"
                if not {"[Content_Types].xml", "_rels/.rels", required} <= names or opposite in names:
                    raise ValueError()
                xml = {}
                budget = {"nodes": 0, "text": 0}
                for entry in entries:
                    if not entry.filename.endswith((".xml", ".rels")):
                        continue
                    raw = archive.read(entry)
                    xml[entry.filename] = _office_xml(raw, budget)
                package_ns = "http://schemas.openxmlformats.org/package/2006/"
                office_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
                main_mime = (
                    MIMES[extension].replace(".document", ".document.main+xml")
                    if extension == "docx"
                    else MIMES[extension] + ".main+xml"
                )
                types = xml["[Content_Types].xml"]
                if types.tag != "{" + package_ns + "content-types}Types" or not any(
                    node.get("PartName") == "/" + required and node.get("ContentType") == main_mime for node in types
                ):
                    raise ValueError()
                relationships = xml["_rels/.rels"]
                if relationships.tag != "{" + package_ns + "relationships}Relationships" or not any(
                    node.get("Type") == office_ns + "/officeDocument"
                    and node.get("Target", "").lstrip("/") == required
                    and node.get("TargetMode") != "External"
                    for node in relationships
                ):
                    raise ValueError()
                root = xml[required]
                if extension == "docx":
                    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                    if root.tag != "{" + namespace + "}document" or root.find("{" + namespace + "}body") is None:
                        raise ValueError()
                else:
                    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                    if root.tag != "{" + namespace + "}workbook" or "xl/_rels/workbook.xml.rels" not in xml:
                        raise ValueError()
                    sheets = root.find("{" + namespace + "}sheets")
                    if sheets is None or not list(sheets):
                        raise ValueError()
                    links = {node.get("Id"): node for node in xml["xl/_rels/workbook.xml.rels"]}
                    for sheet in sheets:
                        link = links.get(sheet.get("{" + office_ns + "}id"))
                        if (
                            link is None
                            or link.get("Type") != office_ns + "/worksheet"
                            or link.get("TargetMode") == "External"
                        ):
                            raise ValueError()
                        target = link.get("Target", "")
                        target = (
                            target.lstrip("/")
                            if target.startswith("/")
                            else posixpath.normpath(posixpath.join("xl", target))
                        )
                        if target not in xml or xml[target].tag != "{" + namespace + "}worksheet":
                            raise ValueError()
    except (ValueError, UnicodeError, zipfile.BadZipFile, KeyError, ET.ParseError, subprocess.TimeoutExpired):
        raise APIError(
            422, "invalid_attachment_file", "文件结构无效或格式不匹配，不能把网页或验证码页作为附件提供"
        ) from None
    except ImportError:
        raise APIError(503, "attachment_validator_unavailable", "附件校验组件暂不可用") from None
    except Exception:
        raise APIError(422, "invalid_attachment_file", "文件结构无效或格式不匹配") from None
    return extension, MIMES[extension]


async def replace_attachment(core, snapshot_id, request, user):
    from .events import idempotent
    from .sources import FetchError, FetchResult, _get, _idempotency, canonical_url, public_source_url, url_has_secret

    _idempotency(request)
    original = _get(core, "snapshots", snapshot_id)
    if not original.get("parent_snapshot_id"):
        raise APIError(422, "attachment_required", "只能为通知的附件提供替代文件")
    source = _get(core, "sources", original["source_id"])
    if original.get("visibility") != "public" or source.get("verification") == "unverified":
        raise APIError(403, "public_source_required", "只能为已核验的公开来源提供授权文件")
    if not source.get("enabled"):
        raise APIError(409, "source_disabled", "来源已停用，不能补充附件")
    try:
        for value in (original["source_url"], source["origin_url"]):
            if public_source_url(value) != canonical_url(value) or url_has_secret(value):
                raise FetchError("private_source")
    except FetchError:
        raise APIError(422, "public_source_required", "不能为私密来源或含凭据的链接提供公开附件") from None
    try:
        form = await request.form(max_files=1, max_fields=1, max_part_size=8192)
    except HTTPException:
        raise APIError(422, "invalid_attachment_form", "请提交一个文件和授权依据") from None
    try:
        if set(form) != {"file", "basis"} or len(form.getlist("file")) != 1 or len(form.getlist("basis")) != 1:
            raise APIError(422, "invalid_attachment_form", "请提交一个文件和授权依据")
        upload = form["file"]
        basis = form["basis"]
        if not isinstance(upload, UploadFile) or not isinstance(basis, str) or not basis.strip() or len(basis) > 2000:
            raise APIError(422, "authorization_basis_required", "请提供不超过 2000 字的授权依据")
        body = await upload.read(MAX_UPLOAD_BYTES + 1)
        kind, mime = await run_in_threadpool(validate_file, body, upload.filename, upload.content_type)
        filename = (upload.filename or "")[:200]
    finally:
        await form.close()
    basis = basis.strip()
    digest = hashlib.sha256(body).hexdigest()
    payload = {"snapshot_id": snapshot_id, "content_hash": digest, "content_kind": kind, "basis": basis}

    def action():
        parent = _get(core, "snapshots", original["parent_snapshot_id"])
        if parent.get("visibility") != "public" or parent["source_id"] != original["source_id"]:
            raise APIError(403, "public_source_required", "只能为公开通知的附件提供授权文件")
        document = _get(core, "documents", parent["document_id"])
        active = document.get("snapshot_attachments", {}).get(parent["snapshot_id"])
        if active is None:
            active = [
                s["snapshot_id"]
                for s in core.store.list("snapshots")
                if s.get("parent_snapshot_id") == parent["snapshot_id"]
                and (core.store.get("documents", s["document_id"]) or {}).get("latest_snapshot_id") == s["snapshot_id"]
            ]
        if snapshot_id not in active:
            raise APIError(409, "attachment_changed", "附件已有更新，请选择当前待核对的附件")
        receipt_id = uid()
        provided_at = now_iso()
        capture = {
            "method": "operator_upload",
            "replacement_receipt_id": receipt_id,
            "replaces_snapshot_id": snapshot_id,
            "provided_at": provided_at,
            "provenance": PROVENANCE,
            "original_verified": False,
        }
        replacement, _ = core.pipeline.sources.persist(
            source,
            FetchResult(canonical_url(original["source_url"]), body, mime),
            original["title"],
            original["published_at"],
            parent["snapshot_id"],
            capture=capture,
            content_kind_override=kind,
        )
        new_id = replacement["snapshot_id"]
        active = [new_id if sid == snapshot_id else sid for sid in active]
        document = core.store.get("documents", parent["document_id"])
        document.setdefault("snapshot_attachments", {})[parent["snapshot_id"]] = list(dict.fromkeys(active))
        from .blob_store import BlobError, blob_store
        from .sources import attachment_access_restricted

        restricted = False
        for sid in active:
            attachment = _get(core, "snapshots", sid)
            if attachment["content_kind"] != "html":
                continue
            try:
                raw = blob_store(core).read(attachment["object_key"], attachment["content_hash"])
                restricted = restricted or attachment_access_restricted(raw, attachment["source_url"])
            except BlobError:
                restricted = True
        if not restricted:
            old = document.get("snapshot_warnings", {}).get(parent["snapshot_id"], [])
            document.setdefault("snapshot_warnings", {})[parent["snapshot_id"]] = [
                w for w in old if w != "attachment_access_restricted"
            ]
        core.store.put("documents", document["id"], document)
        core.pipeline.parse_document({"snapshot_id": new_id})
        candidate = core.pipeline.extract_candidates({"snapshot_id": parent["snapshot_id"]})
        if candidate is None:
            raise APIError(422, "parent_unparseable", "通知正文尚不可解析，请先核对正文")
        result = {
            "snapshot_id": new_id,
            "replaces_snapshot_id": snapshot_id,
            "candidate_id": candidate["candidate_id"],
            "revision": candidate["revision"],
        }
        validate("AttachmentReplacementResponse", result)
        audit = {
            "id": receipt_id,
            "actor_id": user["id"],
            "authorization_basis": basis,
            "source_id": source["id"],
            "replaces_snapshot_id": snapshot_id,
            "snapshot_id": new_id,
            "content_hash": digest,
            "content_kind": kind,
            "filename": filename,
            "created_at": provided_at,
            "candidate_id": candidate["candidate_id"],
            "candidate_revision": candidate["revision"],
        }
        core.store.put("snapshot_replacements", receipt_id, audit, expected_revision=0)
        return result

    # The entire transaction stays on one worker thread, including nested parses.
    return await run_in_threadpool(
        idempotent, core, request, user["id"], "snapshot.replace:" + snapshot_id, payload, action
    )
