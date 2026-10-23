"""Conservative extraction with immutable evidence and revisioned human review."""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString
from fastapi import APIRouter, Request

from . import change_detect, zh_time
from .blob_store import BlobError, blob_store
from .platform import APIError, require_user
from .schema import now_iso, uid, validate
from .sources import (
    FetchError,
    Sources,
    _get,
    _page,
    attachment_access_restricted,
    canonical_url,
    decode_body,
    require_snapshot_read,
)

PARSER_VERSION = "campus-body-1.4"
EXTRACTOR_VERSION = "campus-rules-1.13"
MAX_SEGMENTS, MAX_TEXT, MAX_PAGES, MAX_ARCHIVE_EXPANSION = (
    500,
    500_000,
    100,
    32 * 1024 * 1024,
)
TZ = timezone(timedelta(hours=8))
CONTENT_SELECTORS = (
    "#js_content",
    ".rich_media_content",
    "#vsb_content",
    ".v_news_content",
    "#vsb_content_2",
    '[itemprop="articleBody"]',
    ".article-content",
    ".article-body",
    ".entry-content",
    ".news_content",
    ".article_con",
    "article",
    "main",
)


def _html_error_page(soup, visible):
    """Recognize a CMS failure/error page, which carries no notice.

    Real collection produced a candidate titled 「操作失败！」: the CMS answered a
    detail link with its failure page and the product stored it as a notice.
    Only the *shape* counts — the whole page is one short error sentence and the
    title is one of the failure labels — so a notice that happens to be about a
    system error ("关于选课系统报错的通知") stays a notice.
    """
    if len(visible) > 200:
        return False
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    first = soup.find(["h1", "h2"])
    heading = first.get_text(" ", strip=True) if first else ""
    failure_label = re.compile(
        r"^(?:操作失败|提示信息|系统提示|错误|出错了?|访问出错|访问失败|系统繁忙|请求失败|页面不存在|404(?: not found)?|"
        r"500|403|forbidden|not found|error)(?:[！!。.\s]*)$",
        re.I,
    )
    # The page title and its only heading need not agree ("错误" / "出错了").
    if not any(failure_label.match(label.strip()) for label in (title, heading) if label.strip()):
        return False
    # The page must be nothing but the failure sentence.
    looks_like_failure = re.search(
        r"操作失败|参数错误|请返回|请稍后|页面不存在|无法(?:显示|访问|找到)|暂时不可用|请求的资源|重新操作|出错了?",
        visible,
    )
    return bool(looks_like_failure)


def _html_interstitial(soup):
    """Recognize access pages, not ordinary instructions to log into a system."""
    visible = soup.get_text(" ", strip=True)
    if len(visible) > 2000:
        return False
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    first = soup.find(["h1", "h2"])
    heading = first.get_text(" ", strip=True) if first else ""
    # WeChat answers a notice link from a data-centre IP with a 18 KB
    # "环境异常 / 完成验证后即可继续访问" challenge page. It is an access
    # interstitial, not a notice: publishing it produced candidates titled
    # 「环境异常」 that only consume the reviewer's time.
    wechat_challenge = bool(
        re.search(r"环境异常|完成验证后即可继续访问|请在微信客户端打开链接", visible + "\n" + title + "\n" + heading)
    )
    if wechat_challenge and len(visible) < 600:
        return True
    access_title = bool(
        re.search(
            r"^(?:(?:用户|账号|统一身份|系统)\s*)?(?:登录|认证)$|安全验证|人机验证|访问受限|访问验证|access denied|just a moment|verify.{0,20}human|^sign in$|^login$",
            title + "\n" + heading,
            re.I | re.M,
        )
    )
    password = soup.select_one('input[type="password" i]') is not None
    challenge = any(
        re.search(
            r"captcha|challenge|verify",
            str(node.get("id", "")) + " " + str(node.get("name", "")),
            re.I,
        )
        for node in soup.find_all(["form", "input", "iframe"])
    )
    deleted = bool(
        re.search(
            r"^(?:微信公众平台\s*)?(?:该内容已被发布者删除|此内容因违规无法查看|该内容无法查看|页面不存在|无权访问|访问被拒绝)(?:[。！!\s]|$)",
            visible,
        )
    )
    return deleted or (access_title and (password or challenge or len(visible) < 300))


def _html_lines(node):
    """Flush at block boundaries, never between inline spans of a date or word."""
    lines = []
    buffer = []
    blocks = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "blockquote",
        "pre",
        "table",
        "thead",
        "tbody",
        "tfoot",
    }

    def flush():
        value = " ".join("".join(buffer).split())
        buffer.clear()
        if value:
            lines.append(value)

    def visit(current):
        if isinstance(current, Comment):
            return
        if isinstance(current, NavigableString):
            buffer.append(str(current))
            return
        if current.name == "br":
            flush()
            return
        if current.name == "tr":
            flush()
            # Preserve the material label and its cells as one row of evidence.
            cells = current.find_all(["th", "td"], recursive=False)
            value = (
                " ".join(" ".join(_html_lines(cell)) for cell in cells)
                if cells
                else " ".join(current.get_text("", strip=False).split())
            )
            if value.strip():
                lines.append(value.strip())
            return
        block = current.name in blocks
        if block:
            flush()
        for child in current.children:
            visit(child)
        if block:
            flush()

    visit(node)
    flush()
    return lines


def _article_footer_facts(node):
    """An article's contact/signature is content; a site footer is boilerplate."""
    ids = {selector[1:] for selector in CONTENT_SELECTORS if selector.startswith("#")}
    classes = {selector[1:] for selector in CONTENT_SELECTORS if selector.startswith(".")}
    in_body = any(
        parent.name == "article"
        or parent.get("id") in ids
        or bool(classes.intersection(parent.get("class", [])))
        or parent.get("itemprop") == "articleBody"
        for parent in node.parents
    )
    if not in_body:
        return False
    value = node.get_text(" ", strip=True)
    return bool(
        re.search(
            r"联系人|联系电话|联系方式|电话|邮箱|承办|主办|作者|署名|发布单位|来源\s*[:：]|\b0\d{2,3}[-－— ]?\d{7,8}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
            value,
        )
    )


COLUMN_NAME_TITLES = (
    "培养工作",
    "学位与导师工作",
    "研究生招生",
    "研院简介",
    "非学历继续教育",
    "综合事务",
    "通知公告",
    "学业指导",
    "学生竞赛",
    "竞赛科创",
    "新闻动态",
    "综合新闻",
    "招聘会信息",
    "本科教学",
    "通知",
    "公告",
)

# Order matters: the first match wins, so the narrowest audience wins. Every
# value stays inside the frozen enumeration of campus-contracts/domain-v1
# (academic/assignment/scholarship/competition/activity/employment/meeting/other);
# adding a value such as "admission" would break the v1.5.0 contract, so
# admissions and exam notices reuse activity/academic.
EVENT_TYPE_RULES = (
    # A scholarship/prize notice is about money or an award, not about the
    # ceremony that announces it.
    (r"奖学金|助学金|奖助|奖金", "scholarship"),
    (r"竞赛|比赛|大赛|赛区|参赛", "competition"),
    # A job event names recruiting outright. 宣讲会 is deliberately NOT here:
    # an exchange-programme information session is also a 宣讲会, and filing
    # those under 就业 hid them from the students looking for them (real notice:
    # "2026年暑期剑桥大学移动课堂跨学科学术实践项目宣讲会"). 招聘 / 双选会 /
    # 供需见面 / 校招 stay employment, and 宣讲会 falls through to the activity
    # rule unless one of those words is present.
    (r"就业|招聘|实习|双选会|供需见面|求职|派遣|职业规划|入校招聘|校招", "employment"),
    (r"论文答辩|开题|中期检查|作业", "assignment"),
    (
        # 考试 / 选拔 / 面试 / 转专业 / 学籍 all land in the教务 bucket; the real
        # gs template printed "培养工作" as its headline, so the body text
        # ("公共课考试安排") has to be able to decide too.
        r"考试|考核|笔试|面试|选拔|遴选|选课|学籍|转专业|辅修|专业确认|成绩|缓考|重修|答辩安排",
        "academic",
    ),
    (r"招生|招录|推免|免试攻读|直博|夏令营|研究生招生", "activity"),
    (r"会议|座谈|研讨会|论坛", "meeting"),
    (r"活动|讲座|报告会|报名|项目通知|交流项目|暑期|寒假|冬季|春季|秋季|学期|举办|艺术节|文化节|晚会|演出|展览", "activity"),
)
EVENT_TYPE_FALLBACK = "other"
# A channel name is not a notice; it says nothing about the type. When the
# headline is one of these, the body decides instead of the (useless) title.
EVENT_TYPE_TITLE_MAX = 60


def event_type_values(title, body_text):
    """Classify a notice inside the frozen v1 event_type enumeration.

    The headline is the strongest signal, but the gs template printed the
    channel name ("培养工作") as the <h1>, which used to make every notice in
    that column fall through to "other". So a channel-name headline is treated
    as no evidence at all and the body text decides.

    Body evidence is deliberately *not* used for a headline that is a real
    headline and simply matched no keyword. A long news report mentions 成绩 or
    奖学金 in passing, and letting that decide filed university news under
    教务/学业 — a filter the notice does not belong to. For a student a wrong
    category is worse than "other", because "other" is honest uncertainty while
    a wrong category hides the notice in the wrong list.
    """
    headline = " ".join((title or "").split())
    usable_headline = bool(headline) and headline not in COLUMN_NAME_TITLES and len(headline) <= EVENT_TYPE_TITLE_MAX
    if usable_headline:
        for words, kind in EVENT_TYPE_RULES:
            if re.search(words, headline):
                return kind
        # The page did state what it is; the body cannot override a real title.
        return EVENT_TYPE_FALLBACK
    body = " ".join((body_text or "").split())
    for words, kind in EVENT_TYPE_RULES:
        if re.search(words, body):
            return kind
    return EVENT_TYPE_FALLBACK


# A card title tells a student what the notice is. Some strings that reach the title
# slot carry no such information: page furniture ("总体情况", "下载中心"), a bare notice
# word ("会议通知", "公告"), or a channel/unit name ("人才招聘", "计算机学院").
# Round 14 only refused a fixed list of *known column* names, which missed all of the
# above (see verification/extraction/README.md for the census and the real pages).
GENERIC_CARD_TITLES = frozenset(
    {
        "会议通知", "会议", "通知", "公告", "公示", "活动通知", "讲座通知", "报告会",
        "通知公告", "活动", "总体情况", "下载中心", "更多", "首页", "栏目", "新闻",
        "正文", "内容", "详情", "概况", "简介", "联系我们", "人才招聘", "人事招聘",
    }
)
# A whole title that is nothing but a unit/channel name: short, and ending in an
# organisational noun. A real headline that merely *contains* 学院 (e.g.
# 「机械工程学院2027年推荐…办法」) is longer and does not match.
_CHANNEL_ONLY_TITLE = re.compile(
    r"^[·•\-\s]*[\u4e00-\u9fa5A-Za-z]{0,10}(学院|学部|学系|中心|办公室|研究院|研究所|工作部|处|部|网)$"
)


def title_is_informative(title: str, site_names=()) -> bool:
    """Whether a title is enough for a student to decide whether to open the card."""
    text = " ".join((title or "").split())
    if len(text) < 2:
        return False
    if text in GENERIC_CARD_TITLES:
        return False
    for site in site_names:
        known = " ".join((site or "").split())
        if known and (text == known or text in {part.strip() for part in known.replace("｜", "·").split("·")}):
            return False
    if len(text) <= 14 and _CHANNEL_ONLY_TITLE.match(text):
        return False
    return True


def _clean_listing_title(title: str) -> str:
    """The listing page's own text, minus list decoration and its trailing date.

    This is the notice's real title (the collector read it from the same notice's
    list entry); removing a bullet, a leading index and the date the card already
    shows is formatting, not rewriting.
    """
    text = " ".join((title or "").split())
    text = re.sub(r"^[·•\-\u2022\s]+", "", text)
    text = re.sub(r"^\d{1,3}\s+(?=20\d{2}[-/.]\d{1,2}\s)", "", text)
    text = re.sub(r"\s+20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\s*$", "", text)
    text = re.sub(r"\s+20\d{2}\s*[-–]\s*20\d{2}\s*$", "", text)
    return text.strip()


def choose_card_title(page_title: str, listing_title: str, source_name: str = ""):
    """Pick the card title, using the listing's own title when the page has none.

    Returns ``(title, warning)``. Nothing is ever invented: the fallback is the title
    the collector already read for this same notice, and when even that is unusable the
    page's own text is kept and flagged for review.
    """
    source_names = []
    if source_name:
        source_names.append(source_name)
        source_names.extend(part.strip() for part in source_name.split("·"))
    if title_is_informative(page_title, source_names):
        return " ".join(page_title.split()), ""
    cleaned = _clean_listing_title(listing_title)
    if title_is_informative(cleaned, source_names):
        return cleaned, "title_taken_from_listing"
    if page_title and page_title.strip():
        return " ".join(page_title.split()), "title_unidentifiable"
    if cleaned:
        return cleaned, "title_unidentifiable"
    return "", "title_unidentifiable"


def _document_title_candidates(soup, root):
    """Title candidates in descending trust, most specific first.

    The body container is not authoritative for a title: several CMS templates
    (天津大学研究生院 / gs.tju.edu.cn) print the *channel* name ("培养工作") in an
    <h1> that sits inside the content container, so ``root.select_one("h1")``
    named the whole notice after its column and every notice in that column
    looked identical to a student. CMS article metadata and the document title
    carry the real headline, so they are consulted before any in-body heading.
    """
    values = []
    # A heading inside the article body is the page's own headline when the
    # template is semantic (<article><h1>). It outranks the document title,
    # which often names the site ("学校网站"). Channel-name headings are
    # filtered out later by _choose_title.
    if root is not None:
        for heading in root.find_all(["h1", "h2", "h3"], limit=5):
            values.append(heading.get_text("", strip=False))
    for selector, attribute in (
        ('meta[name="ArticleTitle"]', "content"),
        ('meta[property="og:title"]', "content"),
        ('meta[name="title"]', "content"),
    ):
        node = soup.select_one(selector)
        if node and node.get(attribute):
            values.append(node.get(attribute))
    # A document title is usually "headline-站点名"; keep the headline only.
    if soup.title:
        document_title = " ".join(soup.title.get_text("", strip=False).split())
        headline = re.split(r"\s*[-_|—－]\s*", document_title)[0].strip()
        values.append(headline or document_title)
    for heading in soup.find_all(["h1", "h2"], limit=40):
        # A navigation/header/aside heading names the site or a column.
        if heading.find_parent(["header", "nav", "aside", "footer"]):
            continue
        values.append(heading.get_text("", strip=False))
    return [" ".join(value.split()) for value in values if value and value.strip()]


def _choose_title(soup, root):
    """Pick the headline: the first candidate that actually says what the notice is.

    Round 14 also treated *any* heading under ``nav``/``header``/``aside``/``li`` as a
    channel name. Real 机械学院 / 卫生应急学院 templates wrap the whole article in a
    list item, so their real ``<h1>`` was rejected and a later navigation heading
    ("总体情况", "人事招聘") became the card title — the real pages are fixtures in
    ``verification/extraction/fixtures/``. The judgement is now about the text itself
    (:func:`title_is_informative`), so page furniture never wins over a real headline.
    """
    site_names = set(COLUMN_NAME_TITLES)
    if soup.title:
        document_title = " ".join(soup.title.get_text("", strip=False).split())
        site_names.add(document_title)
        site_names.add(re.split(r"\s*[-_|—－]\s*", document_title)[-1].strip())
    for heading in soup.find_all(["h1", "h2"], limit=40):
        # A heading inside navigation is never the notice's headline, whatever it says.
        if heading.find_parent(["nav", "header", "aside", "footer"]):
            text = " ".join(heading.get_text("", strip=False).split())
            if text:
                site_names.add(text)
    for candidate in _document_title_candidates(soup, root):
        if candidate in site_names:
            continue
        if len(candidate) >= 2 and title_is_informative(candidate, site_names):
            return candidate
    return ""


def html_body_segments(body: bytes, source_url: str):
    """Pure structural extraction shared by the parser and the deployment probe.

    Targets stay literal href values; relative URLs remain available for review
    and are not promoted into evidence-backed absolute entry URLs. No network
    or JavaScript is run.
    """
    if len(body) > 8 * 1024 * 1024 or body.count(b"<") > 100_000:
        return [], "failed", ["html_structure_limit_requires_review"]
    if attachment_access_restricted(body, source_url):
        return [], "failed", ["attachment_access_restricted"]
    soup = BeautifulSoup(decode_body(body), "html.parser")
    if _html_interstitial(soup):
        return [], "failed", ["html_access_interstitial"]
    if _html_error_page(soup, soup.get_text(" ", strip=True)):
        return [], "failed", ["html_error_page"]
    warnings = []
    state = "ok"
    for current in list(
        soup.select(
            'script,style,noscript,iframe,nav,footer,aside,[role="navigation"],[role="contentinfo"],[role="complementary"]'
        )
    ):
        if current.parent is not None:
            if current.name == "footer" and _article_footer_facts(current):
                continue
            current.decompose()
    for current in list(soup.find_all(True)):
        if current.parent is None:
            continue
        style = str(current.get("style", ""))
        hidden = (
            "hidden" in current.attrs
            or str(current.get("aria-hidden", "")).lower() == "true"
            or bool(re.search(r"(?:^|;)\s*display\s*:\s*none\b", style, re.I))
        )
        invisible = bool(re.search(r"(?:^|;)\s*visibility\s*:\s*hidden\b", style, re.I))
        # WeChat's static HTML may wait for JS to reveal #js_content. Keep that
        # supplied text for review, without declaring the hidden page complete.
        if invisible and current.get("id") == "js_content" and not hidden:
            warnings.append("html_visibility_requires_review")
            state = "partial"
        elif hidden or invisible:
            current.decompose()
            continue
        markers = [str(current.get("id", "")), *current.get("class", [])]
        if any(
            re.fullmatch(
                r"related[-_]?(?:posts?|articles?|news|links)?|recommend(?:ed|ations)?|sidebar|breadcrumbs?|pagination|comments?(?:[-_]list)?",
                marker,
                re.I,
            )
            for marker in markers
        ):
            value = current.get_text("", strip=True)
            linked = sum(len(link.get_text("", strip=True)) for link in current.find_all("a"))
            heading = current.find(["h2", "h3", "h4"])
            labelled = heading is not None and bool(
                re.fullmatch(
                    r"相关推荐|相关阅读|相关文章|热门推荐|评论|留言",
                    heading.get_text("", strip=True),
                )
            )
            if labelled or (value and linked / len(value) >= 0.5):
                current.decompose()
    root = None
    for candidate_selector in CONTENT_SELECTORS:
        candidates = [node for node in soup.select(candidate_selector) if node.get_text("", strip=True)]
        if candidates:
            root = max(candidates, key=lambda node: len(node.get_text("", strip=True)))
            break
    if root is not None:
        external_heading = next(
            (heading for heading in soup.find_all("h1") if not heading.find_parent(["header", "nav", "aside"])),
            None,
        )
        header = root.select_one("h1") or soup.select_one(".rich_media_title") or external_heading or soup.title
        chosen_title = _choose_title(soup, root) or (
            " ".join(header.get_text("", strip=False).split()) if header else ""
        )
        lines = _html_lines(root)
        value = root.get_text("", strip=True)
        linked = sum(len(link.get_text("", strip=True)) for link in root.find_all("a"))
        if root.find_all("a") and value and linked / len(value) >= 0.7:
            warnings.append("html_link_collection_requires_review")
            state = "partial"
        if any(
            str(cell.get(attr, "1")) not in {"", "1"}
            for cell in root.find_all(["td", "th"])
            for attr in ("rowspan", "colspan")
        ):
            warnings.append("html_table_spans_require_review")
            state = "partial"
    else:
        # Unknown templates are useful to review, but cannot silently acquire
        # the confidence of a configured article body.
        state = "partial"
        warnings.append("generic_body_fallback_requires_review")
        for current in list(soup.select("header")):
            if current.parent is not None:
                current.decompose()
        header = soup.select_one("h1") or soup.title
        root = soup.body or soup
        chosen_title = _choose_title(soup, root) or (
            " ".join(header.get_text("", strip=False).split()) if header else ""
        )
        try:
            import trafilatura

            extracted = trafilatura.extract(
                str(soup),
                include_comments=False,
                include_tables=True,
                include_links=False,
                favor_recall=True,
            )
        except ImportError:
            extracted = None
            warnings.append("trafilatura_unavailable")
        lines = [line for line in re.split(r"[\r\n]+", extracted or "") if line.strip()] or _html_lines(root)
    title = chosen_title
    body_lines = [line for line in lines if line.strip() and line.strip() != title]
    if not body_lines:
        warning = "html_image_body_requires_ocr" if root.find("img") else "html_empty_body_requires_review"
        return [], "failed", list(dict.fromkeys(warnings + [warning]))
    segments = [{"id": "title", "text": title, "page": None}] if title else []
    segments.extend({"id": f"body:{index}", "text": line, "page": None} for index, line in enumerate(body_lines))
    # A URL printed only in an href remains a literal attribute-value quote.
    # Exclude unsafe/userinfo URLs and keep normalization out of the evidence.
    seen = set()
    printed_urls = set(re.findall(r"https?://[^\s<>“”）)]+", "\n".join(body_lines)))
    for link in root.find_all("a", href=True):
        if len(segments) > MAX_SEGMENTS:
            break
        href = link["href"]
        if not isinstance(href, str) or href in seen or href != href.strip():
            continue
        try:
            canonical_url(urljoin(source_url, href))
        except FetchError:
            continue
        if href.startswith("#"):
            continue
        seen.add(href)
        if href not in printed_urls:
            segments.append({"id": f"link:{len(seen) - 1}", "text": href, "page": None})
        if not re.match(r"^https?://", href, re.I) and re.search(
            r"办理|报名|申请|填报|提交|入口", link.get_text("", strip=True)
        ):
            warnings.append("relative_entry_link_requires_review")
            state = "partial"
    if len(segments) > MAX_SEGMENTS or sum(len(segment["text"]) for segment in segments) > MAX_TEXT:
        segments = segments[:MAX_SEGMENTS]
        remaining = MAX_TEXT
        for segment in segments:
            segment["text"] = segment["text"][:remaining]
            remaining -= len(segment["text"])
        segments = [segment for segment in segments if segment["text"]]
        state = "partial"
        warnings.append("text_limit_requires_review")
    return segments, state, list(dict.fromkeys(warnings))


def original_date_patterns():
    """Reuse the original triage script's pattern declarations without its I/O/CLI."""
    path = Path(__file__).with_name("date_patterns.py")
    names = {"DATE_PATTERNS", "WEEKDAY_PATTERN", "TIME_PATTERN", "HOUR_PATTERN"}
    env = {"re": re}
    if path.is_file():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nodes = [
            n
            for n in tree.body
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)
        ]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), env)
    return env


def previous_parsed_text(core, document_id, current_snapshot_id):
    """Text of the most recently observed *other* parsed capture of a document.

    Returns ``(None, None)`` when there is nothing to compare against, so a
    first capture never reports a spurious change.
    """
    document = core.store.get("documents", document_id) or {}
    observed = document.get("snapshot_observed_at") or {}
    for candidate_id in sorted(observed, key=lambda key: observed.get(key) or "", reverse=True):
        if candidate_id == current_snapshot_id:
            continue
        parsed = core.store.get("parsed", candidate_id)
        if parsed and parsed.get("segments"):
            return candidate_id, "\n".join(segment["text"] for segment in parsed["segments"])
    return None, None


PATTERNS = original_date_patterns()
DATE_PATTERNS = PATTERNS.get(
    "DATE_PATTERNS",
    [re.compile(r"(?:(?P<year>20\d{2})[年./-])?(?P<month>\d{1,2})[月./-](?P<day>\d{1,2})日?")],
)
DATE_PATTERNS = [
    re.compile(
        r"(?P<year>20(?:[ \t]+\d[ \t]*\d|\d[ \t]+\d))\s*年\s*(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?"
    ),
    *DATE_PATTERNS,
]
WEEKDAY_PATTERN = PATTERNS.get(
    "WEEKDAY_PATTERN",
    re.compile(r"(?:(?P<prefix>本|这|下)周|周|星期)(?P<weekday>[一二三四五六日天])"),
)
TIME_PATTERN = PATTERNS.get("TIME_PATTERN", re.compile(r"(?P<hour>\d{1,2})[:：](?P<minute>\d{2})"))
HOUR_PATTERN = PATTERNS.get("HOUR_PATTERN", re.compile(r"(?P<hour>\d{1,2})点(?P<half>半)?"))
# 下午三点 carries the same instruction as 下午3点. The period is deliberately not
# part of the match so the existing lookbehind that reads 上午/下午 still applies,
# and 第三点 is excluded because it is an enumeration, not a clock time.
CN_HOUR_PATTERN = re.compile(r"(?<!第)(?P<hour>[一二两三四五六七八九十]{1,3})\s*点(?P<half>半)?")
# "24时" is the same clock notation as "24:00"/"24点" -- it once fell through to
# date precision while the colon form rolled to the next day at 00:00, so one
# notice displayed two different answers depending on notation. Durations are
# written 24小时/24个小时, where a 小 always separates the digits from the 时;
# a lookbehind for 小 plus a lookahead for 辰/间 keeps those spellings out.
HOUR_SHI_PATTERN = re.compile(r"(?<!小)(?<!\d)(?P<hour>\d{1,2})\s*时(?![辰间])")
# A submission instruction that names a concrete date and then the word 前.
# Observed verbatim on real Tianjin University pages on 2026-10-13 (see
# verification/source-reachability-2026-10-13.json): "请考生所在学院于2026年12月15日前统一
# 报送", "最晚于26年9月18日16:30前…上报", "应于2026年9月24日（报名结束前）提出". The plain
# 请于 cue misses these because other words sit between 请 and 于, and the date itself may
# carry a two-digit year, a clock, or a parenthesised phrase before 前.
BEFORE_DATE_CUE = (
    r"于[^，。；！？\n]{0,12}\d+[日号][^，。；！？\n]{0,12}?(?:之)?前(?![后期夕往来者年天日])"
)
# The same instruction with the date carrying its own due wording and no 于.
# Observed verbatim in the 127 pending candidates of the real queue (2026-10-21
# sweep, verification/extraction/sample-missing-deadline.json): "10月16日12:00前，
# 有意向申请本项目的同学请登录…" (2027年春季学期交换生第二批), "8月31日之前将人事档案
# 原件寄送至我校", "2026年6月30日前未办理结业、肄业相关手续的，学校将依据…作退学处理".
# All five carried no cue word at all, so the whole sentence was invisible to the
# extractor and the candidate reached the review queue without a deadline.
# 前 inside a compound (前后/前期/夕/前往/来/者/前几天) says nothing about a deadline
# and is deliberately excluded — "3月15日前后举行" is not a date to hand in anything.
TRAILING_BEFORE_CUE = (
    r"(?:\d{2,4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
    r"[^，。；！？\n]{0,10}?(?:之)?前(?![后期夕往来者年天日])"
)
# A schedule sentence says when something happens, not when it is due. Observed
# verbatim in the notice staged for operator cycle 1
# (deploy/operator/staging/zs-notices/强基计划校考工作安排.txt): "校考于6月30日…举行",
# "报到时间为6月30日13:00-13:30", "可于6月28日12:00起登录". The extractor returned no
# date for any of the three, so a student reading "校考安排" never saw the 校考 time.
# None of them carries 截止/前 wording, so the deadline cues never reach them; and a
# schedule is not a due date, so this cue opens the gate and labels the row
# event_time without ever joining DEADLINE_CUE.
SCHEDULE_CUE = (
    r"(?:(?:报到|校考|笔试|面试|体测|体检|复试|初试|军训|开学|入校|来校|打印准考证)时间"
    r"|于[^，。；！？\n]{0,14}\d+[日号][^，。；！？\n]{0,10}起"
    r"|(?:校考|考试|笔试|面试|体测|体检|军训|开学|来校)[^，。；！？\n]{0,6}于[^，。；！？\n]{0,6}\d+[日号])"
)
DATE_CUE = (
    r"截止|截至|请于|报名时间|申请时间|提交时间|前提交|前完成|时间[:：]|活动时间|项目时间|会议时间|考试时间|公示期|逾期系统关闭|"
    + BEFORE_DATE_CUE
    + r"|"
    + TRAILING_BEFORE_CUE
    + r"|"
    + SCHEDULE_CUE
)
EVENT_TIME_CUE = r"(?:活动|项目|会议|考试|讲座|举办|举行)时间|(?:^|\s)时间[:：]|" + SCHEDULE_CUE
DEADLINE_CUE = (
    r"截止|截至|报名时间|申请时间|提交时间|请于|前提交|前完成|公示期|逾期系统关闭|"
    + BEFORE_DATE_CUE
    + r"|"
    + TRAILING_BEFORE_CUE
)
NEGATED_ACTION = r"无需|无须|不必|不需(?:要)?|不用|不要求|免(?:予)?(?:提交|提供|上传|携带)"
# Wording that obliges the reader. Real notices say 「请在系统内报名」
# 「请提交申请表」「需上传材料」 far more often than 「请于…」, and leaving those
# out made almost every published notice report action_required=None, which the
# student feed rendered as 「待核实」 on 100% of rows. Kept in one place because
# the candidate is re-validated against the same wording (a True without an
# obligation word is refused as fabricated).
OBLIGATION_WORDS = re.compile(r"必须|务必|须|请|需要|应当|应于|需")
# Wording that says the action is up to the reader. Kept in one place for the
# same reason: a False without one of these is refused as fabricated.
OPTIONAL_WORDS = re.compile(r"自愿|欢迎|有意向|有意参加|可报名")
LEVEL_MAP = {"本科": "undergraduate", "硕士": "master", "博士": "doctor"}


def education_values(text):
    """Keep positive mentions separate from exclusions and degree requirements."""
    levels = []
    for clause in re.split(r"[，,；;。]", text):
        for word, value in LEVEL_MAP.items():
            if word not in clause:
                continue
            excluded = bool(
                re.search(r"(?:不含|不包括|不面向|不接受|除|非)\s*" + word, clause)
                or re.search(
                    word + r"[^，。；]{0,14}(?:不得|不能|不可|不在|不符合|除外|不予|禁止)",
                    clause,
                )
            )
            # A degree to be possessed is not evidence of the applicant's level.
            degree_only = bool(re.search(word + r"(?:毕业证|学位|学历)", clause)) and not re.search(
                word + r"(?:研究)?生", clause
            )
            if not excluded and not degree_only:
                levels.append(value)
    return list(dict.fromkeys(levels))


CANCEL_VERBS = re.compile(r"取消|终止|停止举办|不再举办")
# "取消" that acts on a form control is a UI instruction, not a cancelled event.
CANCEL_UI_OBJECT = re.compile(r"^(?:勾选|选项|选择|点击|按钮|预约|订单|报名|申请|提交|设置|订阅|关注|支付|操作|该|此)")
# A clause that *ends* in 取消 is ambiguous: "原活动取消" cancels the event,
# "点击删除按钮取消" tells the reader which button to press, "（三）预约取消"
# names a booking action. What precedes the verb decides.
CANCEL_UI_CONTEXT = re.compile(r"点击|按(?:下)?|按钮|随时|随意|自行|如何|怎么|重新|手动|预约|订单|勾选|选择|删除|修改")
# "可取消…" states what the reader may do, not that the event is cancelled.
CANCEL_CAPABILITY = re.compile(r"(?:可|可以|能够|支持|允许)\s*$")
# A clause that reserves a right is not an act: "集团有权根据…因素，调整、取消或
# 终止个别岗位的招聘工作". The reservation may sit in the clause before the verb,
# so the test looks at the whole sentence.
CANCEL_DISCLAIMER = re.compile(r"有权|最终解释权|保留[^。；]{0,10}权利")
# The organiser listing its options ("调整、取消或终止") describes what it may do,
# not what happened.
CANCEL_ALTERNATIVES = re.compile(
    r"(?:调整|变更|修改|取消|终止|停止|暂缓|中止)\s*[、或和及]\s*(?:调整|变更|修改|取消|终止|停止|暂缓|中止)"
)


def cancellation_value(text):
    """An actual cancellation of the event — not a hypothetical, not a UI action.

    Real notices kept being mislabelled by a bare 取消 anywhere in the body:
    "如遇疫情变化，考点可能取消当次考试" (conditional), "可取消已提交的申请"
    (the student cancels their own submission), "点击删除按钮取消" and
    "取消勾选…选项" (form controls), "（三）预约取消" (cancelling a booking).
    A cancellation candidate must be linked to the original event, so a false
    positive made a real notice permanently unpublishable.
    """
    for sentence in re.split(r"[。]", text):
        # A reservation of rights covers the whole sentence, however many commas
        # separate the right from the verb.
        if CANCEL_DISCLAIMER.search(sentence):
            continue
        for clause in re.split(r"[，,；;]", sentence):
            if re.search(r"资格|学籍|成绩|录取资格|处罚|违纪", clause):
                continue
            if re.search(r"(?:未|没有|并未|不予|无需|不会|不再)\s*(?:取消|终止|停止)", clause):
                continue
            # A conditional or hypothetical cancellation is not a cancellation.
            if re.search(r"如(?:遇|需|有|果|不)|若|一旦|万一|可能|或将|是否|视情况", clause):
                continue
            match = CANCEL_VERBS.search(clause)
            if not match:
                continue
            if CANCEL_ALTERNATIVES.search(clause):
                continue
            after = clause[match.end() :].lstrip(" ：:，,、的了")
            before = clause[: match.start()]
            if CANCEL_CAPABILITY.search(before):
                continue
            if after and CANCEL_UI_OBJECT.match(after):
                continue
            if not after and CANCEL_UI_CONTEXT.search(before):
                continue
            return True
    return False


def _split_time_clauses(text):
    """Independent time clauses retain their own role and cannot borrow a clock."""
    clauses = re.split(r"[，,；;]\s*(?=(?:" + DATE_CUE + r"))", text)
    if len(clauses) > 1 and all(re.search(DATE_CUE, clause) for clause in clauses):
        return clauses
    return None


def _publication_anchor(published_at):
    """Shanghai anchor for relative dates; an unreadable stamp anchors nothing."""
    if not published_at:
        return None
    try:
        anchor = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return anchor.replace(tzinfo=TZ) if anchor.tzinfo is None else anchor.astimezone(TZ)


def _nearest_year_date(month, day, raw_year, anchor):
    """Resolve a month/day to the nearest year; an explicit year stays literal."""
    year = int(re.sub(r"\s+", "", raw_year)) if raw_year else (anchor.year if anchor else None)
    if not year:
        return None
    years = [year] if raw_year else [year - 1, year, year + 1]
    candidates = []
    for candidate_year in years:
        try:
            candidates.append(datetime(candidate_year, month, day, tzinfo=TZ))
        except ValueError:
            pass
    if not candidates:
        return None
    if raw_year:
        return candidates[0]
    # Like zh_time._resolve_month, use the nearest occurrence, measured in
    # calendar days here; explicit years stay literal.
    return min(
        candidates,
        key=lambda candidate: abs((candidate.date() - anchor.date()).days),
    )


def _literal_date_spans(text, anchor):
    """Every literal date outside URLs, each resolved to its nearest year."""
    found = []
    spans = []
    url_spans = [(m.start(), m.end()) for m in re.finditer(r"https?://[^\s<>]+", text)]
    for pattern in DATE_PATTERNS:
        for m in pattern.finditer(text):
            if any(a <= m.start() < b for a, b in url_spans):
                continue
            if any(a <= m.start() < b for a, b in spans):
                continue
            if pattern.pattern.startswith(r"(?<!\d)") and not re.search(
                r"时间|截止|日期|前|报名", text[max(0, m.start() - 20) : m.end() + 20]
            ):
                continue
            raw_year = m.groupdict().get("year")
            value = _nearest_year_date(int(m["month"]), int(m["day"]), raw_year, anchor)
            if value and not raw_year and abs((value.date() - anchor.date()).days) > 183:
                value = None
            spans.append((m.start(), m.end()))
            found.append((m.start(), m.end(), value, raw_year))
    return found


def _expand_range_endpoints(text, found):
    """Chinese ranges often omit the endpoint month/year: 10月10日至12日."""
    expanded = {start: (start, end, value) for start, end, value, _ in found}
    for _, end, value, _ in found:
        short = re.match(
            r"\s*(?:至|到|[—–－～~-])\s*(?:(?P<month>\d{1,2})\s*月\s*)?(?P<day>\d{1,2})\s*[日号]",
            text[end:],
        )
        if not short:
            continue
        short_start = end + short.start("month" if short["month"] else "day")
        short_end = end + short.end()
        existing = next((i for i in found if end <= i[0] < short_end), None)
        endpoint = None
        if value:
            month = int(short["month"]) if short["month"] else value.month
            year = value.year + (1 if short["month"] and month < value.month else 0)
            try:
                endpoint = datetime(year, month, int(short["day"]), tzinfo=TZ)
                if endpoint < value:
                    endpoint = None
            except ValueError:
                pass
        if existing:
            # Inherit only from an explicitly connected range start, never a guess
            # from the publication year of an isolated month/day.
            if not existing[3]:
                expanded[existing[0]] = (existing[0], existing[1], endpoint)
        else:
            expanded[short_start] = (short_start, short_end, endpoint)
    return list(expanded.values())


def _append_weekday_spans(text, found, anchor):
    """Weekday words; a weekday labelling a nearby date is not a second deadline."""
    for m in WEEKDAY_PATTERN.finditer(text):
        # A weekday immediately following a calendar date is a label for that
        # date, not a second relative deadline anchored to publication.
        if not m.groupdict().get("prefix") and any(
            0 <= m.start() - end <= 4 and not text[end : m.start()].strip(" \t（(") for _, end, _ in found
        ):
            continue
        value = None
        if anchor:
            weekdays = {
                "一": 0,
                "二": 1,
                "三": 2,
                "四": 3,
                "五": 4,
                "六": 5,
                "日": 6,
                "天": 6,
            }
            delta = weekdays[m["weekday"]] - anchor.weekday()
            if m.groupdict().get("prefix") == "下":
                delta += 7
            elif delta < 0 and m.groupdict().get("prefix") not in {"本", "这"}:
                delta += 7
            value = (anchor + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
        found.append((m.start(), m.end(), value))


def _append_relative_day_spans(text, found, anchor):
    """今天/明天/后天/今晚 are publication-relative day offsets."""
    for phrase, delta in [("今天", 0), ("明天", 1), ("后天", 2), ("今晚", 0)]:
        if phrase in text:
            p = text.index(phrase)
            value = (
                (anchor + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0) if anchor else None
            )
            found.append((p, p + len(phrase), value))


def _collapse_explicit_range(found, text, is_event_time):
    """The end of an explicitly delimited range is the deadline, not its start."""
    if (
        not is_event_time
        and len(found) == 2
        and re.fullmatch(
            r"\s*(?:(?:上午|下午|晚上)?\s*\d{1,2}(?::\d{2}|点(?:半)?)?\s*)?(?:至|到|[—–－～~-])\s*",
            text[found[0][1] : found[1][0]],
        )
    ):
        return found[-1:]
    return found


def _clock_offset(tail):
    """The clock time written after a date, in digits or Chinese numerals."""
    clock = TIME_PATTERN.search(tail)
    hourmatch = HOUR_PATTERN.search(tail) if not clock else None
    shimatch = HOUR_SHI_PATTERN.search(tail) if not clock and not hourmatch else None
    cnhourmatch = CN_HOUR_PATTERN.search(tail) if not clock and not hourmatch and not shimatch else None
    clockmatch = clock or hourmatch or shimatch or cnhourmatch
    if clock:
        hour, minute = int(clock["hour"]), int(clock["minute"])
    elif hourmatch:
        hour, minute = int(hourmatch["hour"]), 30 if hourmatch["half"] else 0
    elif shimatch:
        hour, minute = int(shimatch["hour"]), 0
    elif cnhourmatch:
        parsed = zh_time.cn_number(cnhourmatch["hour"])
        hour, minute = (int(parsed), 30 if cnhourmatch["half"] else 0) if parsed is not None else (None, None)
    else:
        hour = minute = None
    if hour is not None and "下午" in tail[: clockmatch.start()] and hour < 12:
        hour += 12
    if hour is not None and "上午" in tail[: clockmatch.start()] and hour == 12:
        hour = 0
    return hour, minute


def _deadline_kind(text, is_event_time):
    """Event time wins; otherwise the school or a college owns the deadline."""
    if is_event_time:
        return "event_time"
    return "college_deadline" if re.search(r"学院|学部|班级", text) else "school_deadline"


def _deadline_row(text, value, hour, minute, kind, evidence_id):
    """One deadline row; an impossible clock time drops the date, never guesses."""
    # 24:00 is how Chinese notices name the end of a day, not an impossible hour:
    # it is the next day at 00:00. Refusing it as impossible discarded the date
    # as well, so a notice reading 11月30日24:00 reached the student as
    # 截止时间待确认. Every other out-of-range clock is still refused.
    if hour == 24 and minute == 0 and value is not None:
        value, hour, minute = value + timedelta(days=1), 0, 0
    elif hour is not None and (hour > 23 or minute > 59):
        value = None
    at = day = None
    precision = "unknown"
    if value:
        if hour is not None:
            at = value.replace(hour=hour, minute=minute).isoformat()
            precision = "minute"
        else:
            day = value.date().isoformat()
            precision = "date"
    return {
        "kind": kind,
        "raw_text": text,
        "precision": precision,
        "at": at,
        "date": day,
        "timezone": "Asia/Shanghai",
        "evidence_ids": [evidence_id],
    }


# Section headings routinely carry a deadline cue while stating no date:
# "2. 网上报名及截止时间", "【招生截止时间】", "报名流程及截止时间". The real
# deadline lives in the body under the heading, usually in the same notice.
# Publishing the heading as a second "待确认" row tells the student the
# opposite of the truth -- there IS a confirmed date (the 13 heading rows in
# the shipped sample all belong to notices that also carry the real date).
HEADING_PREFIX = re.compile(r"^\s*(?:[（(]?\d+\s*[)）.、：:]|[一二三四五六七八九十]+\s*[、.．]|【[^】]{1,24}】)")
# A bare noun phrase naming a time, with no claim about it: 流程及截止时间.
TIME_NOUN_TAIL = re.compile(
    r"(?:流程|安排|说明|须知|方式|办法)?[及和与、]?"
    r"(?:截止|报名|申请|审核|缴费|公示)?(?:流程|安排|说明)?[及和与]?"
    r"(?:截止|报名|申请|审核|缴费|公示)?(?:流程|安排|说明)?[及和与]?(?:截止|报名|申请|审核|缴费|公示)时间[】)]?$"
)
# Words that turn a phrase into a statement about the deadline (or an action
# on it). "截止时间另行通知" is a real claim and must keep its unknown row;
# a heading never contains these.
DEADLINE_CLAIM_WORDS = re.compile(
    r"请|于|前|内|提交|报送|公布|另行|截至|截止\s*[:：]|需|必须|登录|系统|网站|逾期|不受理|为准"
)


def _is_heading_deadline_reference(raw_text):
    """True when a cue-bearing line is a table-of-contents heading, not a claim."""
    text = (raw_text or "").strip()
    if not text or len(text) > 30:
        # Long sentences are prose about a deadline, never a heading label.
        return False
    if DEADLINE_CLAIM_WORDS.search(text):
        return False
    if HEADING_PREFIX.match(text):
        return True
    return len(text) <= 14 and bool(TIME_NOUN_TAIL.search(text))


def _evidence_span(text, start, end, limit=80):
    """The shortest quote around a date that still reads as the original claim.

    ``raw_text`` is what a student (or a reviewer) uses to check the notice, so
    it must be a fragment that contains the date verbatim -- not the whole
    paragraph, which made "查看原文" point at a sentence the date was buried in.
    The span stops at the nearest clause boundary on each side. Brackets are
    deliberately NOT a boundary: cutting at "（" removes the cue that precedes
    it ("…请特别留意报名时间（第一阶段…") and the quote stops being able to
    prove its own date. ``date_values`` re-derives the deadline from whatever
    span is chosen and widens it when the cut lost the cue, so a quote can never
    become unverifiable -- it can only become as wide as the evidence needs.
    """
    left = max(start, 0)
    while left > 0 and text[left - 1] not in "，。；！？|｜\n\r":
        left -= 1
    right = min(end, len(text))
    while right < len(text) and text[right] not in "，。；！？|｜\n\r":
        right += 1
    span = text[left:right].strip()
    if len(span) <= limit:
        return span
    # A single clause longer than the limit: keep a window that still contains
    # the date, centred on it.
    date_centre = (start + end) // 2 - left
    half = max(1, limit // 2)
    window_left = max(0, date_centre - half)
    window = text[left + window_left : left + window_left + limit].strip()
    return window


def _drop_heading_deadline_placeholders(deadlines):
    """Drop dateless placeholder rows when the real date is already present.

    Two shapes reach this: a section heading that names a deadline without
    stating one ("2. 网上报名及截止时间"), and a body sentence that mentions the
    word but carries no date ("报名流程和要求等按照…简章执行"). Neither is a
    second deadline; both told the student "待确认" while the notice did state a
    confirmed date of the same kind.

    The row is only dropped when a parseable date of the *same kind* exists,
    and a line making a real statement about the deadline ("截止时间另行通知",
    "逾期不予受理") is kept. A notice whose only signal is dateless keeps its
    unknown row: "needs review" must never silently become "no deadline".
    """
    parsed_kinds = {row["kind"] for row in deadlines if row["precision"] != "unknown"}
    if not parsed_kinds:
        return deadlines
    return [
        row
        for row in deadlines
        if row["precision"] != "unknown"
        or row["kind"] not in parsed_kinds
        or (
            not _is_heading_deadline_reference(row.get("raw_text"))
            and DEADLINE_CLAIM_WORDS.search(row.get("raw_text") or "")
        )
    ]


def _sentence_span(text, start, end):
    """The sentence around a match, for when the clause alone lost the cue."""
    left = max(start, 0)
    while left > 0 and text[left - 1] not in "。；！？\n\r":
        left -= 1
    right = min(end, len(text))
    while right < len(text) and text[right] not in "。；！？\n\r":
        right += 1
    return text[left:right].strip()


def _deadline_rows_for(text, anchor, evidence_id, quote=None):
    """Derive the deadline rows of ``text`` paired with their match offsets.

    Kept separate from :func:`date_values` so the caller can ask "would this
    shorter quote still yield the same deadline?" without re-entering the
    shrinking logic.
    """
    if not re.search(DATE_CUE, text):
        return []
    found = _literal_date_spans(text, anchor)
    found.sort()
    found = _expand_range_endpoints(text, found)
    _append_weekday_spans(text, found, anchor)
    _append_relative_day_spans(text, found, anchor)
    if not found:
        if re.search(r"周|星期|待定|另行|尽快|月底|前|截止", text):
            found = [(0, 0, None)]
        else:
            return []
    found.sort()
    is_event_time = bool(re.search(EVENT_TIME_CUE, text)) and not re.search(DEADLINE_CUE, text)
    found = _collapse_explicit_range(found, text, is_event_time)
    kind = _deadline_kind(text, is_event_time)
    result = []
    for index, (start, end, value) in enumerate(found):
        next_start = found[index + 1][0] if index + 1 < len(found) else len(text)
        tail = text[end : min(end + 20, next_start)]
        tail = re.split(r"[，,；;]", tail, maxsplit=1)[0]
        hour, minute = _clock_offset(tail)
        row = _deadline_row(quote if quote is not None else text, value, hour, minute, kind, evidence_id)
        result.append((start, end, row))
    return result


def _row_key(row):
    return (row["kind"], row["precision"], row["at"], row["date"])


def date_values(text, published_at, evidence_id):
    """Only time-bearing instructions; absent publication never anchors relative time."""
    clauses = _split_time_clauses(text)
    if clauses is not None:
        return [value for clause in clauses for value in date_values(clause, published_at, evidence_id)]
    anchor = _publication_anchor(published_at)
    derived = _deadline_rows_for(text, anchor, evidence_id)
    if not derived:
        return []
    # A deadline quote has to be checkable by a reader, so it is the clause the
    # date came from rather than the whole paragraph. It also has to stand on
    # its own: the candidate is re-validated against its own raw_text, and a
    # clause that cut away the date cue ("请于…前") would be rejected as a
    # fabricated date. So the narrowest span that still re-derives the same
    # deadline wins, widening to the sentence and finally the paragraph.
    rederived = {}
    result = []
    for start, end, row in derived:
        candidates = []
        for span in (_evidence_span(text, start, end), _sentence_span(text, start, end), text):
            if span and span not in candidates:
                candidates.append(span)
        chosen = row["raw_text"]
        for candidate in candidates:
            if candidate not in rederived:
                rederived[candidate] = {
                    _row_key(value) for _, _, value in _deadline_rows_for(candidate, anchor, evidence_id, candidate)
                }
            if _row_key(row) in rederived[candidate]:
                chosen = candidate
                break
        result.append({**row, "raw_text": chosen})
    return list({(i["kind"], i["precision"], i["at"], i["date"]): i for i in result}.values())


def material_values(sentence):
    """Return an explicit list/object span; a material mention is insufficient."""
    clauses = re.split(
        r"[，,]\s*(?=(?:"
        + NEGATED_ACTION
        + r"|活动时间|会议时间|考试时间|报名时间|申请时间|截止|截至|请|并|于|在|逾期|办理|报名|入口|网址|链接|https?://))",
        sentence,
    )
    if len(clauses) > 1:
        return list(dict.fromkeys(value for clause in clauses for value in material_values(clause)))
    if re.search(NEGATED_ACTION, sentence):
        return []
    form = re.search(
        r"(?:填写|填报)\s*(?:附件\s*)?(《[^》]*(?:申请表|报名表|确认表)[^》]*》)",
        sentence,
    )
    if form:
        return [form.group(1)]
    explicit = re.search(r"材料(?:清单|包括|为|如下)?\s*[:：]\s*(.+)", sentence)
    match = explicit or re.search(r"(?:提交|上传|提供|携带)\s*(.+)", sentence)
    if not match:
        return []
    value = match.group(1)
    # Keep enumeration commas; stop before another instruction, address or link.
    value = re.split(
        r"[，,]\s*(?:请|并|于|在|逾期|截止|办理|报名|入口|网址|链接|https?://)|(?:至|到)(?=[^，,]{0,20}(?:办公室|邮箱|系统|平台|老师))",
        value,
        maxsplit=1,
    )[0].strip(" ：:，,")
    value = value.rstrip("。；;")
    if not value or re.match(r"另行|待定|以.+为准|详见|见附件|见通知|暂无", value):
        return []
    if not explicit:
        prefix = sentence[: match.start()]
        if prefix and not re.search(r"请|须|务必|需|要求|应当|报名|申请人", prefix):
            return []
        if not re.search(
            r"申请表|报名表|确认表|成绩单|身份证|学生证|证明|简历|推荐信|陈述|方案|说明|作品"
            # A category word ("申请材料", "报名材料") is a real deliverable even
            # though it names no single document: the notice still tells the
            # student to hand something in. Real notice: "需于2026年4月15日前
            # 提交电子版申请材料". A bare material *mention* stays rejected --
            # the submission verb and the obligation prefix above are required.
            r"|(?:申请|报名|申报|评审|证明|纸质|电子版?|相关|以下|其它|其他)材料|材料(?:清单|目录)",
            value,
        ):
            return []
    return [value]


class Pipeline:
    def __init__(self, core, fetcher=None):
        self.core = core
        self.sources = Sources(core, fetcher)
        self.handlers = {
            "fetch_source": self.sources.fetch_source,
            "parse_document": self.parse_document,
            "extract_candidates": self.extract_candidates,
        }

    def parsed(self, snapshot_id):
        return self.core.store.get("parsed", snapshot_id)

    def segments(self, snapshot_ids):
        result = []
        for sid in dict.fromkeys(snapshot_ids):
            parsed = self.parsed(sid)
            if parsed:
                result += [{"snapshot_id": sid, **s} for s in parsed["segments"]]
        return result

    def get_accepted_candidate(self, candidate_id, revision):
        candidate = self.core.store.get("candidate_versions", f"{candidate_id}:{revision}")
        if not candidate or candidate["review_status"] != "accepted":
            return None
        # Old extractors could set accepted without a human decision. A queued
        # legacy revision must not become publishable just because of that flag.
        reviewed = any(
            review.get("candidate_id") == candidate_id
            and review.get("revision") == revision
            and review.get("decision") == "accept"
            for review in self.core.store.list("reviews")
        )
        return candidate if reviewed else None

    def register_evidence(self, snapshot_id, segment_id, quote):
        snapshot = _get(self.core, "snapshots", snapshot_id)
        parsed = _get(self.core, "parsed", snapshot_id)
        segment = next((s for s in parsed["segments"] if s["id"] == segment_id), None)
        if not segment or not quote or quote not in segment["text"]:
            raise APIError(422, "invalid_evidence", "证据必须是原始片段中的连续文本")
        eid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{snapshot_id}:{segment_id}:{quote}"))
        existing = self.core.store.get("evidence", eid)
        if existing:
            return existing
        evidence = dict(
            id=eid,
            snapshot_id=snapshot_id,
            source_id=snapshot["source_id"],
            source_url=snapshot["source_url"],
            segment_id=segment_id,
            page=segment["page"],
            quote=quote,
            content_hash=snapshot["content_hash"],
        )
        if snapshot.get("capture"):
            evidence["capture"] = dict(snapshot["capture"])
        validate("Evidence", evidence)
        with self.core.store.transaction():
            self.core.store.put("evidence", eid, evidence)
            # Parsed evidence can grow by immutable IDs; original text/hash is unchanged.
            parsed = _get(self.core, "parsed", snapshot_id)
            if not any(e["id"] == eid for e in parsed["evidence"]):
                parsed["evidence"].append(evidence)
                self.core.store.put("parsed", snapshot_id, parsed)
        return evidence

    def parse_document(self, payload):
        sid = payload["snapshot_id"]
        snapshot = _get(self.core, "snapshots", sid)
        existing = self.parsed(sid)
        document = self.core.store.get("documents", snapshot["document_id"]) or {}
        input_warnings = document.get("snapshot_warnings", {}).get(sid, [])
        parse_input = {
            "parser_version": PARSER_VERSION,
            "snapshot_warnings": input_warnings,
        }
        if (
            existing
            and existing["parser_version"] == PARSER_VERSION
            and self.core.store.get("parse_inputs", sid) == parse_input
        ):
            return existing
        try:
            body = blob_store(self.core).read(snapshot["object_key"], snapshot["content_hash"])
        except BlobError as exc:
            raise FetchError(exc.code) from None
        segments = []
        warnings = []
        state = "ok"
        kind = snapshot["content_kind"]
        if input_warnings:
            warnings.extend(input_warnings)
            state = "partial"
        try:
            if body.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
                state = "failed"
                warnings.append("unsupported_legacy_office_requires_review")
            elif kind == "html":
                segments, html_state, html_warnings = html_body_segments(body, snapshot["source_url"])
                warnings.extend(html_warnings)
                if html_state != "ok":
                    state = html_state
            elif kind == "pdf":
                # Isolate complex PDF decompression/OCR requirements from the web process.
                script = """
import io,json,sys
try:
 import resource
 resource.setrlimit(resource.RLIMIT_AS,(768*1024*1024,768*1024*1024))
except ImportError: pass
from pypdf import PdfReader
reader=PdfReader(io.BytesIO(sys.stdin.buffer.read()),strict=False)
if reader.is_encrypted or len(reader.pages)>100: raise ValueError('unsupported_pdf')
pages=[]; remaining=500000
for page in reader.pages:
 text=page.extract_text() or ''
 pages.append(text[:remaining]); remaining-=len(pages[-1])
 if remaining<=0: break
sys.stdout.write(json.dumps({'pages':pages,'truncated':remaining<=0}))
"""
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                completed = subprocess.run(
                    [sys.executable, "-c", script],
                    input=body,
                    capture_output=True,
                    timeout=20,
                    creationflags=flags,
                )
                if completed.returncode or len(completed.stdout) > MAX_TEXT * 8:
                    raise ValueError("invalid_pdf")
                decoded = json.loads(completed.stdout)
                if decoded["truncated"]:
                    state = "partial"
                    warnings.append("pdf_text_limit_requires_review")
                for page_number, text in enumerate(decoded["pages"], 1):
                    if not text.strip():
                        state = "partial"
                        warnings.append(f"ocr_required_page_{page_number}")
                    for i, line in enumerate(text.splitlines()):
                        if line.strip():
                            segments.append(
                                {
                                    "id": f"page:{page_number}:{i}",
                                    "text": " ".join(line.split()),
                                    "page": page_number,
                                }
                            )
                if not segments:
                    state = "failed"
                    warnings.append("no_extractable_text_ocr_not_configured")
            elif kind in {"docx", "xlsx"}:
                archive = zipfile.ZipFile(io.BytesIO(body))
                entries = archive.infolist()
                if (
                    len(entries) > 1000
                    or sum(e.file_size for e in entries) > MAX_ARCHIVE_EXPANSION
                    or any(
                        e.file_size > MAX_ARCHIVE_EXPANSION or (e.compress_size and e.file_size / e.compress_size > 200)
                        for e in entries
                    )
                ):
                    raise ValueError("archive_expansion_limit")
                import xml.etree.ElementTree as ET

                selected = (
                    ["word/document.xml"]
                    if kind == "docx"
                    else [
                        e.filename
                        for e in entries
                        if e.filename == "xl/sharedStrings.xml" or re.match(r"xl/worksheets/sheet\d+\.xml", e.filename)
                    ]
                )
                for name in selected:
                    raw = archive.read(name)
                    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
                        raise ValueError("unsafe_xml")
                    root = ET.fromstring(raw)
                    for i, node in enumerate(root.iter()):
                        if node.tag.rsplit("}", 1)[-1] in {"p", "row"}:
                            text = " ".join(t.text or "" for t in node.iter() if t.tag.rsplit("}", 1)[-1] in {"t", "v"})
                            if text.strip():
                                segments.append({"id": f"{name}:{i}", "text": text, "page": None})
                if kind == "xlsx":
                    state = "partial"
                    warnings.append("xlsx_cell_indexes_require_review")
            elif kind == "image":
                state = "failed"
                warnings.append("ocr_not_configured")
            else:
                for i, line in enumerate(decode_body(body).splitlines()):
                    if line.strip():
                        segments.append(
                            {
                                "id": f"text:{i}",
                                "text": " ".join(line.split()),
                                "page": None,
                            }
                        )
            if len(segments) > MAX_SEGMENTS or sum(len(s["text"]) for s in segments) > MAX_TEXT:
                segments = segments[:MAX_SEGMENTS]
                remaining = MAX_TEXT
                for s in segments:
                    s["text"] = s["text"][:remaining]
                    remaining -= len(s["text"])
                segments = [s for s in segments if s["text"]]
                state = "partial"
                warnings.append("text_limit_requires_review")
            if not segments:
                state = "failed"
                warnings.append("empty_document")
        except subprocess.TimeoutExpired:
            state = "failed"
            warnings.append("parser_timeout")
        except (ImportError, ValueError, zipfile.BadZipFile) as error:
            state = "failed"
            warnings.append(
                "parser_unavailable" if isinstance(error, ImportError) else "unsupported_or_invalid_document"
            )
        except Exception:
            state = "failed"
            warnings.append("parser_failed")
        current_segments = {segment["id"]: segment["text"] for segment in segments}
        retained_evidence = [
            e for e in (existing or {}).get("evidence", []) if e["quote"] in current_segments.get(e["segment_id"], "")
        ]
        # Compare before validating so the operator-facing parse record and the
        # student-facing event history are derived from exactly one diff.
        previous_id, previous_text = previous_parsed_text(self.core, snapshot["document_id"], sid)
        change = None
        if previous_text is not None:
            report = change_detect.diff(previous_text, "\n".join(segment["text"] for segment in segments))
            if report["changed"]:
                change = {
                    "previous_snapshot_id": previous_id,
                    "summary": change_detect.summarize(report),
                    "counts": report["counts"],
                    "kinds": report["kinds"],
                    "truncated": report["truncated"],
                }
        result = {
            "snapshot_id": sid,
            "parser_version": PARSER_VERSION,
            "state": state,
            "segments": segments,
            "evidence": retained_evidence,
            "warnings": list(dict.fromkeys(warnings)),
            "change": change,
        }
        validate("ParsedDocument", result)
        with self.core.store.transaction():
            self.core.store.put("parsed", sid, result)
            self.core.store.put("parse_inputs", sid, parse_input)
            if change:
                document = self.core.store.get("documents", snapshot["document_id"]) or {}
                document.setdefault("change_summaries", {})[sid] = change
                self.core.store.put("documents", snapshot["document_id"], document)
            self.core.store.emit(
                "content.parsed",
                sid,
                1,
                snapshot_id=sid,
                source_id=snapshot["source_id"],
            )
        return result

    def _candidate_validation(self, candidate):
        validate("ExtractionCandidate", candidate)
        valid_ids = {e["id"] for e in candidate["evidence"]}
        for e in candidate["evidence"]:
            stored = self.core.store.get("evidence", e["id"])
            if not stored or stored != e or e["snapshot_id"] not in candidate["snapshot_ids"]:
                raise APIError(422, "invalid_evidence", "证据与快照不匹配")
        for ids in (
            list(candidate["field_evidence"].values())
            + [candidate["audience"]["evidence_ids"]]
            + [d["evidence_ids"] for d in candidate["deadlines"]]
        ):
            if any(eid not in valid_ids for eid in ids):
                raise APIError(422, "invalid_evidence", "字段证据不属于当前候选")
        fields = candidate["field_evidence"]
        if not fields["title"] or not any(
            candidate["title"] in e["quote"] for e in candidate["evidence"] if e["id"] in fields["title"]
        ):
            raise APIError(422, "invalid_title_evidence", "标题缺少原文依据")
        for name in ("action_steps", "materials", "entry_urls"):
            if candidate[name] and not fields[name]:
                raise APIError(422, "missing_field_evidence", "字段缺少原文依据")
            for value in candidate[name]:
                if not any(value in e["quote"] for e in candidate["evidence"] if e["id"] in fields[name]):
                    raise APIError(422, "fabricated_field", "字段必须来自原文连续片段")
        if candidate["action_required"] is not None and not fields["action_required"]:
            raise APIError(422, "missing_action_evidence", "行动判断缺少依据")
        if candidate["proposed_status"] == "cancelled" or candidate["change_kind"] == "cancel":
            if not fields["status"] or not any(
                cancellation_value(e["quote"]) for e in candidate["evidence"] if e["id"] in fields["status"]
            ):
                raise APIError(422, "missing_cancel_evidence", "取消状态缺少原文依据")
        for d in candidate["deadlines"]:
            if not any(d["raw_text"] in e["quote"] for e in candidate["evidence"] if e["id"] in d["evidence_ids"]):
                raise APIError(422, "invalid_deadline_evidence", "截止时间原文不匹配")
            # An operator may choose among evidence-derived dates, never invent one.
            if d["precision"] != "unknown":
                legal = []
                for e in candidate["evidence"]:
                    if e["id"] in d["evidence_ids"]:
                        snapshot = _get(self.core, "snapshots", e["snapshot_id"])
                        primary = date_values(d["raw_text"], snapshot["published_at"], e["id"])
                        legal += primary
                        legal += [
                            value
                            for value in zh_time.supplement(e["quote"], snapshot["published_at"], [], e["id"])
                            if value["raw_text"] == d["raw_text"]
                        ]
                if not any(
                    (i["kind"], i["precision"], i["at"], i["date"]) == (d["kind"], d["precision"], d["at"], d["date"])
                    for i in legal
                ):
                    raise APIError(422, "fabricated_deadline", "时间及其用途无法从原文解析")
        audience = candidate["audience"]
        aq = "；".join(e["quote"] for e in candidate["evidence"] if e["id"] in audience["evidence_ids"])
        levels = set(education_values(aq))
        for level in audience["education_levels"]:
            if level not in levels:
                raise APIError(422, "fabricated_audience", "学生层次缺少正向适用依据")
        campuses = {"weijinlu": "卫津路", "beiyangyuan": "北洋园"}
        for campus in audience["campuses"]:
            if campus not in campuses or campuses[campus] not in aq:
                raise APIError(422, "fabricated_audience", "校区缺少依据")
        for year in audience["entry_years"]:
            if not re.search(str(year) + r"\s*级", aq):
                raise APIError(422, "fabricated_audience", "入学年级缺少依据")
        colleges = {c["id"]: c["name"] for c in (self.core.store.get("catalog", "tju") or {}).get("colleges", [])}
        for college in audience["college_ids"]:
            name = colleges.get(college)
            if not name or (
                name not in aq
                and name.split("-")[-1] not in aq
                and not (name.startswith("智能与计算学部-") and "智能与计算学部" in aq)
            ):
                raise APIError(422, "fabricated_audience", "学院身份缺少依据")
        if audience["certainty"] == "explicit" and not audience["evidence_ids"]:
            raise APIError(422, "missing_audience_evidence", "适用人群缺少证据")
        action_quotes = "；".join(e["quote"] for e in candidate["evidence"] if e["id"] in fields["action_required"])
        if candidate["action_required"] is True and not OBLIGATION_WORDS.search(action_quotes):
            raise APIError(422, "fabricated_action_requirement", "必办判断缺少明确义务依据")
        if candidate["action_required"] is False and not (
            OPTIONAL_WORDS.search(action_quotes) or re.search(r"成功举办|回顾|获评|获奖|召开|签署", action_quotes)
        ):
            raise APIError(422, "fabricated_action_requirement", "可选或资讯判断缺少依据")
        if candidate["target_event_id"]:
            if not getattr(self.core, "events", None) or not self.core.events.get_public(candidate["target_event_id"]):
                raise APIError(422, "invalid_target_event", "关联事项不存在")

    def extract_candidates(self, payload):
        snapshot = _get(self.core, "snapshots", payload["snapshot_id"])
        if snapshot["parent_snapshot_id"]:
            snapshot = _get(self.core, "snapshots", snapshot["parent_snapshot_id"])
        sid = snapshot["snapshot_id"]
        parent = self.parse_document({"snapshot_id": sid})
        if not parent or not parent["segments"]:
            return None
        parent_document = self.core.store.get("documents", snapshot["document_id"]) or {}
        active = parent_document.get("snapshot_attachments", {}).get(sid)
        children = [
            s
            for s in self.core.store.list("snapshots")
            if s["parent_snapshot_id"] == sid
            and (
                s["snapshot_id"] in active
                if active is not None
                else (self.core.store.get("documents", s["document_id"]) or {}).get("latest_snapshot_id")
                == s["snapshot_id"]
            )
        ]
        for child in children:
            self.parse_document({"snapshot_id": child["snapshot_id"]})
        ids = [sid] + [s["snapshot_id"] for s in children]
        all_segments = self.segments(ids)
        candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "candidate:" + sid))
        previous = self.core.store.get("candidates", candidate_id)
        candidate_input = {}
        for active_snapshot in [snapshot] + children:
            active_id = active_snapshot["snapshot_id"]
            parsed = self.parsed(active_id)
            document = self.core.store.get("documents", active_snapshot["document_id"]) or {}
            candidate_input[active_id] = {
                "parser_version": parsed["parser_version"],
                "state": parsed["state"],
                "warnings": parsed["warnings"],
                "observation": document.get("snapshot_observations", {}).get(active_id, 0),
                "observed_at": document.get("snapshot_observed_at", {}).get(active_id) or active_snapshot["fetched_at"],
            }
        old_input = self.core.store.get("candidate_inputs", candidate_id)
        if (
            previous
            and set(previous["snapshot_ids"]) == set(ids)
            and previous["extractor_version"] == EXTRACTOR_VERSION
            and old_input == candidate_input
        ):
            return previous
        next_revision = previous["revision"] + 1 if previous else 1
        stored_revision = self.core.store.revision("candidates", candidate_id)
        evidence = []
        field = {
            k: []
            for k in (
                "title",
                "status",
                "action_required",
                "action_steps",
                "materials",
                "entry_urls",
            )
        }
        title_segment = next(
            (s for s in all_segments if s["snapshot_id"] == sid and s["id"] == "title"),
            all_segments[0],
        )
        title = title_segment["text"][:300]
        e = self.register_evidence(title_segment["snapshot_id"], title_segment["id"], title)
        evidence.append(e)
        field["title"].append(e["id"])
        actions = []
        materials = []
        urls = []
        deadlines = []
        warnings = list(parent["warnings"])
        audience_evidence = []
        if previous and previous["extractor_version"] != EXTRACTOR_VERSION:
            warnings.append("extractor_upgrade_requires_review")
        if (
            previous
            and set(previous["snapshot_ids"]) == set(ids)
            and old_input is not None
            and old_input != candidate_input
        ):
            reactivated = any(
                old_input.get(i, {}).get("observation", 0) != candidate_input[i]["observation"] for i in ids
            )
            warnings.append(
                "document_reactivation_requires_review" if reactivated else "parsed_input_changed_requires_review"
            )
        audience = {
            "education_levels": [],
            "college_ids": [],
            "entry_years": [],
            "campuses": [],
            "conditions_text": None,
            "evidence_ids": [],
            "certainty": "unknown",
        }
        cancelled = False
        required = None
        conditions = []
        conditional_audience = False
        for segment in all_segments:
            if segment["id"].startswith("link:"):
                # Attribute values prove an entry URL, never an audience,
                # instruction or deadline that happens to appear in its path.
                if re.match(r"^https?://", segment["text"], re.I):
                    e = self.register_evidence(segment["snapshot_id"], segment["id"], segment["text"])
                    evidence.append(e)
                    urls.append(segment["text"])
                    field["entry_urls"].append(e["id"])
                continue
            for sentence in re.split(r"[。；\n]", segment["text"]):
                sentence = sentence.strip()
                if not sentence:
                    continue
                relevant = bool(
                    re.search(
                        r"截止|截至|请于|报名|申请|提交|填写|材料|附件|全体|本科|硕士|研究生|博士|\d{4}级|校区|取消|终止|不再举办|https?://|时间[:：]|公示期",
                        sentence,
                    )
                )
                if not relevant:
                    continue
                e = self.register_evidence(segment["snapshot_id"], segment["id"], sentence)
                evidence.append(e)
                eid = e["id"]
                if cancellation_value(sentence):
                    cancelled = True
                    field["status"].append(eid)
                if re.search(r"更正为|调整为|延期至|以本[^，。；]{0,20}为准", sentence):
                    warnings.append("correction_requires_review")
                for action_clause in re.split(r"[，,]", sentence):
                    if re.search(NEGATED_ACTION, action_clause):
                        continue
                    if re.search(
                        r"请于|务必|必须|应当|需(?:要)?提交|须提交|报名|填写|上传|缴费|提交",
                        action_clause,
                    ):
                        actions.append(action_clause)
                        field["action_steps"].append(eid)
                        field["action_required"].append(eid)
                        if OPTIONAL_WORDS.search(action_clause):
                            if required is None:
                                required = False
                        elif OBLIGATION_WORDS.search(action_clause):
                            required = True
                if segment["id"] != "title" and segment["text"].strip() != title.strip():
                    values = material_values(sentence)
                    if values:
                        materials.extend(values)
                        field["materials"].append(eid)
                for url in re.findall(r"https?://[^\s<>“”）)]+", sentence):
                    urls.append(url.rstrip("，,；;。"))
                    field["entry_urls"].append(eid)
                published_at = _get(self.core, "snapshots", segment["snapshot_id"])["published_at"]
                primary = date_values(sentence, published_at, eid)
                deadlines += primary
                # Unknown placeholders and distinct literal dates must not hide
                # relative phrases or their missing-anchor review reasons.
                derived = zh_time.supplement(sentence, published_at, primary, eid)
                if derived:
                    deadlines += derived
                    warnings.append("relative_time_requires_review")
                for entry in zh_time.candidates(sentence, published_at):
                    warnings.append("relative_time_requires_review")
                    warnings.append(entry["rule"])
                    if entry.get("review_reason"):
                        warnings.append(entry["review_reason"])
                levels = education_values(sentence)
                years = [int(y) for y in re.findall(r"(20\d{2})级", sentence)]
                campuses = [
                    ident
                    for name, ident in [
                        ("卫津路", "weijinlu"),
                        ("北洋园", "beiyangyuan"),
                    ]
                    if name in sentence
                ]
                colleges = [
                    c["id"]
                    for c in (self.core.store.get("catalog", "tju") or {}).get("colleges", [])
                    if c["name"] in sentence or c["name"].split("-")[-1] in sentence
                ]
                if "智能与计算学部" in sentence and not colleges:
                    colleges = [
                        c["id"]
                        for c in (self.core.store.get("catalog", "tju") or {}).get("colleges", [])
                        if c["name"].startswith("智能与计算学部-")
                    ]
                restricted = bool(
                    re.search(
                        r"仅限|只限|不含|不包括|不得申请|不能申请|不可申请|除[^，。；]{0,20}外|须符合|符合[^，。；]{0,20}条件|申请条件|申请资格",
                        sentence,
                    )
                )
                if restricted:
                    conditional_audience = True
                    warnings.append("audience_conditions_require_review")
                if (
                    levels
                    or years
                    or campuses
                    or colleges
                    or restricted
                    or "全体同学" in sentence
                    or "全体学生" in sentence
                ):
                    audience["education_levels"] += levels
                    audience["entry_years"] += years
                    audience["campuses"] += campuses
                    audience["college_ids"] += colleges
                    audience_evidence.append(eid)
                    conditions.append(sentence)
        if not field["action_required"]:
            if re.search(r"成功举办|回顾|获评|获奖|召开|签署", title):
                required = False
                field["action_required"] = field["title"].copy()
        audience.update(
            conditions_text="；".join(dict.fromkeys(conditions)) or None,
            evidence_ids=list(dict.fromkeys(audience_evidence)),
            certainty="partial" if conditional_audience else "explicit" if audience_evidence else "unknown",
        )
        for k in ("education_levels", "entry_years", "campuses", "college_ids"):
            audience[k] = list(dict.fromkeys(audience[k]))
        # A heading that carries the deadline cue but no date is not a second
        # deadline claim; the real date sits in the body under it. Drop it only
        # when the same kind is already parseable -- see the helper for why the
        # heading-only notice keeps its unknown row.
        deadlines = _drop_heading_deadline_placeholders(deadlines)
        if any(d["precision"] == "unknown" for d in deadlines):
            warnings.append("ambiguous_deadline_requires_review")
        if len({(d["kind"], d["at"], d["date"]) for d in deadlines if d["precision"] != "unknown"}) > 1:
            warnings.append("multiple_dates_require_review")
        if any((self.parsed(i) or {}).get("state") != "ok" for i in ids):
            warnings.append("partial_attachment_or_body_requires_review")
        if any(
            (self.core.store.get("snapshots", i) or {}).get("capture", {}).get("method") == "operator_upload"
            for i in ids
        ):
            warnings.append("operator_upload_requires_review")
        if not children and any(
            re.search(
                r"(?:详见|见|参见|查看|下载|填写|提交)[^。；\n]{0,25}附件|附件[^。；\n]{0,12}(?:清单|要求|条件|申请表|报名表)",
                s["text"],
            )
            for s in all_segments
        ):
            warnings.append("referenced_attachment_missing_requires_review")
        if re.search(r"更正|勘误|延期|变更|补充通知", title):
            warnings.append("correction_requires_review")
        if required is None:
            warnings.append("action_requirement_unknown")
        source = _get(self.core, "sources", snapshot["source_id"])
        if source["verification"] == "unverified":
            warnings.append("unverified_source_requires_review")
        # Cancellation/update target linking is always reviewed; no fuzzy auto-cancel.
        if cancelled:
            warnings.append("cancellation_target_requires_review")
        # Extraction only proposes facts; even a clear notice needs a human
        # decision before the accepted outbox event may authorize publication.
        review_required = True
        # The headline decides when it says something; a polluted channel-name
        # headline ("培养工作") falls back to the body instead of "other".
        event_type = event_type_values(
            title,
            " ".join(s["text"] for s in all_segments if s["id"] != "title"),
        )
        candidate = {
            "candidate_id": candidate_id,
            "revision": next_revision,
            "review_status": "pending",
            "change_kind": "cancel" if cancelled else "update" if previous else "new",
            "target_event_id": previous.get("target_event_id") if previous else None,
            "proposed_status": "cancelled" if cancelled else "active",
            "snapshot_ids": ids,
            "extractor_version": EXTRACTOR_VERSION,
            "title": title,
            "event_type": event_type,
            "action_required": required,
            "action_steps": list(dict.fromkeys(actions)),
            "materials": list(dict.fromkeys(materials)),
            "entry_urls": list(dict.fromkeys(urls)),
            "deadlines": list({json.dumps(d, sort_keys=True): d for d in deadlines}.values()),
            "audience": audience,
            "evidence": list({e["id"]: e for e in evidence}.values()),
            "field_evidence": {k: list(dict.fromkeys(v)) for k, v in field.items()},
            "review_required": review_required,
            "warnings": list(dict.fromkeys(warnings)),
        }
        self._candidate_validation(candidate)
        with self.core.store.transaction():
            self.core.store.put("candidates", candidate_id, candidate, expected_revision=stored_revision)
            self.core.store.put("candidate_inputs", candidate_id, candidate_input)
            self.core.store.put(
                "candidate_inputs",
                f"{candidate_id}:{next_revision}",
                candidate_input,
                expected_revision=0,
            )
            self.core.store.put(
                "candidate_versions",
                f"{candidate_id}:{next_revision}",
                candidate,
                expected_revision=0,
            )
        return candidate

    def review(self, candidate_id, body, user):
        validate("ReviewDecision", body)
        with self.core.store.transaction():
            current = _get(self.core, "candidates", candidate_id)
            stored_revision = self.core.store.revision("candidates", candidate_id)
            if current["revision"] != body["expected_candidate_revision"]:
                raise APIError(409, "revision_conflict", "候选已被其他审核人更新")
            candidate = json.loads(json.dumps(body["replacement_candidate"] or current))
            if (
                candidate["candidate_id"] != candidate_id
                or candidate["revision"] != current["revision"]
                or set(candidate["snapshot_ids"]) != set(current["snapshot_ids"])
            ):
                raise APIError(422, "invalid_replacement", "修正不能更换候选身份、版本或快照")
            self._candidate_validation(candidate)
            if (
                body["decision"] == "accept"
                and candidate["change_kind"] == "cancel"
                and not candidate["target_event_id"]
            ):
                raise APIError(422, "cancel_target_required", "取消通知必须关联原事项")
            candidate.update(
                revision=current["revision"] + 1,
                review_status="accepted" if body["decision"] == "accept" else "rejected",
                review_required=False,
            )
            validate("ExtractionCandidate", candidate)
            self.core.store.put("candidates", candidate_id, candidate, expected_revision=stored_revision)
            self.core.store.put(
                "candidate_versions",
                f"{candidate_id}:{candidate['revision']}",
                candidate,
            )
            frozen_input = self.core.store.get("candidate_inputs", f"{candidate_id}:{current['revision']}")
            if frozen_input is None:
                frozen_input = self.core.store.get("candidate_inputs", candidate_id)
            if frozen_input is not None:
                self.core.store.put(
                    "candidate_inputs",
                    f"{candidate_id}:{candidate['revision']}",
                    frozen_input,
                    expected_revision=0,
                )
            review_id = uid()
            self.core.store.put(
                "reviews",
                review_id,
                {
                    "id": review_id,
                    "candidate_id": candidate_id,
                    "revision": candidate["revision"],
                    "reviewer_id": user["id"],
                    "decision": body["decision"],
                    "reason": body["reason"],
                    "created_at": now_iso(),
                },
            )
            if body["decision"] == "accept":
                self.core.store.emit(
                    "candidate.accepted",
                    candidate_id,
                    candidate["revision"],
                    candidate_id=candidate_id,
                    candidate_revision=candidate["revision"],
                )
        return candidate


def make_router(core):
    router = APIRouter()

    @router.get("/admin/reviews")
    def reviews(request: Request, cursor: str | None = None, limit: int = 20):
        require_user(request, roles={"admin", "operator"})
        return _page(
            [c for c in core.store.list("candidates") if c["review_status"] == "pending"],
            cursor,
            limit,
        )

    @router.post("/admin/reviews/{candidate_id}/decision")
    async def review(candidate_id: str, request: Request):
        user = require_user(request, roles={"admin", "operator"}, write=True)
        return core.pipeline.review(candidate_id, await request.json(), user)

    @router.get("/snapshots/{snapshot_id}/parsed")
    def parsed(snapshot_id: str):
        require_snapshot_read(core, snapshot_id)
        return _get(core, "parsed", snapshot_id)

    @router.get("/admin/snapshots/{snapshot_id}/parsed")
    def admin_parsed(snapshot_id: str, request: Request):
        require_user(request, roles={"admin", "operator"})
        _get(core, "snapshots", snapshot_id)
        return _get(core, "parsed", snapshot_id)

    @router.get("/evidence/{evidence_id}")
    def evidence(evidence_id: str):
        value = _get(core, "evidence", evidence_id)
        require_snapshot_read(core, value["snapshot_id"])
        return value

    return router
