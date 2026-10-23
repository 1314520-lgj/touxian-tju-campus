"""Anti-scraping landing pages must never become a notice.

WeChat answers a notice link fetched from a data-centre IP with a challenge
page ("环境异常 / 完成验证后即可继续访问"). The product must refuse it: it is
an access interstitial, not the article. The address safety check is not
relaxed anywhere -- these tests only assert that *more* is rejected.
"""

from server.campus.content import html_body_segments
from server.campus.sources import FetchError, _reject_redirected_off_pattern

WECHAT_CHALLENGE = """<html><head><title></title></head><body>
<div class="weui-msg"><h2>环境异常</h2>
<p>当前环境异常，完成验证后即可继续访问。</p><a href="/mp/appmsg/show">去验证</a></div>
</body></html>""".encode()

WECHAT_PUBLISHER_DELETED = """<html><head><title></title></head><body>
<h2>该内容已被发布者删除</h2></body></html>""".encode()

ORDINARY_NOTICE_WITH_LOGIN_INSTRUCTION = """<html><head><title>选课通知-教务处</title></head><body>
<div id="vsb_content"><h1>2026 春季选课通知</h1>
<p>请于2026年11月1日前登录教务系统完成选课。</p>
<p>登录后进入"我的选课"页面，逾期不再受理。</p></div></body></html>""".encode()


def test_wechat_challenge_page_is_an_interstitial():
    segments, state, warnings = html_body_segments(WECHAT_CHALLENGE, "https://mp.weixin.qq.com/s/abc")
    assert segments == []
    assert state == "failed"
    assert "html_access_interstitial" in warnings


def test_publisher_deleted_page_is_still_an_interstitial():
    _, state, warnings = html_body_segments(WECHAT_PUBLISHER_DELETED, "https://mp.weixin.qq.com/s/abc")
    assert state == "failed"
    assert "html_access_interstitial" in warnings


def test_notice_telling_the_reader_to_log_in_is_not_an_interstitial():
    """An instruction to log into a system is content, not an access page."""
    segments, state, warnings = html_body_segments(
        ORDINARY_NOTICE_WITH_LOGIN_INSTRUCTION, "https://oaa.tju.edu.cn/info/1/2.htm"
    )
    assert "html_access_interstitial" not in warnings
    assert state in {"ok", "partial"}
    assert any("选课" in segment["text"] for segment in segments)


def test_redirect_off_the_article_pattern_is_rejected():
    """A challenge landing page must not become a snapshot."""
    config = {"article_url_pattern": r"^https://mp\.weixin\.qq\.com/s(?:[/?#]|$)"}
    try:
        _reject_redirected_off_pattern(
            "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha?poc_token=x&target_url=y",
            "https://mp.weixin.qq.com/s/abc",
            config,
        )
    except FetchError as error:
        assert error.code == "article_redirected_off_pattern"
    else:  # pragma: no cover - the guard must reject
        raise AssertionError("a redirected challenge page was accepted")


def test_url_still_matching_the_pattern_is_accepted():
    config = {"article_url_pattern": r"^https://mp\.weixin\.qq\.com/s(?:[/?#]|$)"}
    # Must not raise: the landing URL is still the article itself.
    _reject_redirected_off_pattern(
        "https://mp.weixin.qq.com/s/CSniYqr1LfSYobIeCZA6nw",
        "https://mp.weixin.qq.com/s/CSniYqr1LfSYobIeCZA6nw",
        config,
    )


def test_source_without_a_pattern_is_not_restricted():
    """No declared pattern means no opinion; behaviour stays as before."""
    _reject_redirected_off_pattern("https://example.org/anything", "https://example.org/x", {})


def test_stable_url_outside_the_pattern_is_not_a_redirect():
    """RSS items and non-standard detail paths keep working.

    Only a *change of address* is judged. A page served at the URL that was
    requested was never redirected, so the source's article pattern does not
    apply to it and the fetch must not be refused.
    """
    config = {"article_url_pattern": r"^https://oaa\.tju\.edu\.cn/info/\d+/\d+\.htm$"}
    _reject_redirected_off_pattern("https://public.test/one", "https://public.test/one", config)


def test_patternless_request_that_redirects_still_passes():
    """A redirect is only judged when the source declares an article pattern."""
    _reject_redirected_off_pattern("https://cdn.test/page", "https://example.org/x", {})

