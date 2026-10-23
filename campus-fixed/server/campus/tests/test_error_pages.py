"""An error page is not a notice.

Real collection produced a candidate titled 「操作失败！」 — the CMS answered a
detail link with its failure page, and the product stored it as a notice. A
student who opens such a row learns nothing; the reviewer has to reject it by
hand. Recognising the shapes requires no new content rules: these pages say
nothing about a notice, so they must never become one.
"""

import pytest

from server.campus.content import html_body_segments


def page(title, body):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("操作失败！", "<p>操作失败！</p><p>请返回重试。</p>"),
        ("错误", "<h1>出错了</h1><p>页面无法显示。</p>"),
        ("404 Not Found", "<h1>404</h1><p>您访问的页面不存在。</p>"),
        ("系统繁忙", "<p>系统繁忙，请稍后再试。</p>"),
        ("提示信息", "<p>参数错误，请重新操作。</p>"),
        ("访问出错", "<p>您请求的资源暂时不可用。</p>"),
    ],
)
def test_an_error_page_is_refused(title, body):
    segments, state, warnings = html_body_segments(page(title, body), "https://oaa.tju.edu.cn/info/1/2.htm")
    assert segments == []
    assert state == "failed"
    assert "html_error_page" in warnings


def test_a_notice_that_mentions_an_error_is_still_a_notice():
    """The subject matter is not the shape: a notice about an error stays a notice."""
    segments, state, warnings = html_body_segments(
        page(
            "关于选课系统报错的通知",
            "<div id='vsb_content'><h1>关于选课系统报错的通知</h1>"
            "<p>近期部分同学选课时系统提示操作失败，请于2026年11月1日前重新提交。</p></div>",
        ),
        "https://oaa.tju.edu.cn/info/1/2.htm",
    )
    assert "html_error_page" not in warnings
    assert state in {"ok", "partial"}
    assert any("选课" in segment["text"] for segment in segments)


def test_a_normal_short_notice_is_not_an_error_page():
    segments, state, warnings = html_body_segments(
        page("停水通知", "<div id='vsb_content'><h1>停水通知</h1><p>2026年10月20日8:00-12:00停水。</p></div>"),
        "https://hq.tju.edu.cn/info/1/2.htm",
    )
    assert "html_error_page" not in warnings
    assert segments
