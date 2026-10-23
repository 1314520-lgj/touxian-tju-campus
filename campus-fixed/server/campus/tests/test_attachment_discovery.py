"""Attachment discovery must look at the notice body, not the whole page.

Real 天津大学 notice pages carry a navigation block whose 125 ``download.jsp``
links belong to other notices. The collector scanned every ``<a href>`` on the
page, queued the first four, and every one of them answered with a captcha — so
one notice produced four "access restricted" snapshots that have nothing to do
with it, and the real attachment (if any) was never reached. 177 of the 363
stored snapshots were this noise.

The rule: only links inside the article body count. A page whose body has no
attachment simply has no attachment.
"""

import pytest

from server.campus.sources import FetchResult, discover_attachments

NOTICE = """<html><body>
<div class="nav"><a href="/system/_content/download.jsp?wbfileid=NAV1">其他通知附件一</a>
<a href="/system/_content/download.jsp?wbfileid=NAV2">其他通知附件二</a>
<a href="/system/_content/download.jsp?wbfileid=NAV3">其他通知附件三</a></div>
<div id="vsb_content"><h1>关于选课的通知</h1>
<p>请于2026年11月1日前完成选课。</p>
<p><a href="/system/_content/download.jsp?wbfileid=REAL">附件1：选课操作说明.pdf</a></p>
</div>
<div class="footer"><a href="/system/_content/download.jsp?wbfileid=FOOT">页脚下载</a></div>
</body></html>"""

NO_ATTACHMENT = """<html><body>
<div class="nav"><a href="/system/_content/download.jsp?wbfileid=NAV1">导航附件</a></div>
<div id="vsb_content"><h1>停水通知</h1><p>2026年10月20日8:00-12:00停水。</p></div>
</body></html>"""


def _discover(html, url="https://cic.tju.edu.cn/info/1041/5346.htm"):
    return discover_attachments(FetchResult(url, html.encode(), "text/html"))


def test_only_body_links_become_attachments():
    found = _discover(NOTICE)
    urls = [url for url, _ in found]
    assert len(urls) == 1, urls
    assert "wbfileid=REAL" in urls[0]


def test_navigation_download_links_are_not_attachments():
    urls = [url for url, _ in _discover(NOTICE)]
    for noise in ("NAV1", "NAV2", "NAV3", "FOOT"):
        assert not any(noise in url for url in urls), f"{noise} leaked in"


def test_a_notice_without_body_attachments_has_none():
    assert _discover(NO_ATTACHMENT) == []


def test_the_attachment_title_is_kept():
    found = _discover(NOTICE)
    assert "选课操作说明" in found[0][1]


def test_a_body_link_to_a_real_file_is_found():
    html = """<html><body><div id="vsb_content"><h1>通知</h1>
    <p><a href="/files/plan.xlsx">计划表</a></p></div></body></html>"""
    found = _discover(html)
    assert len(found) == 1
    assert found[0][0].endswith("/files/plan.xlsx")
