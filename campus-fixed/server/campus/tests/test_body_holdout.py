"""Independent synthetic layouts written before reading the new body extractor.

These are small structural holdouts, not a real campus accuracy estimate.
"""

import pytest

from server.campus.content import html_body_segments

CASES = [
    (
        "wordpress",
        '<nav>2027年12月31日热门活动</nav><div class="entry-content"><p>请于2026年10月22日前提交申请表。</p><aside>相关阅读：2028年1月1日截止</aside></div>',
        ["2026年10月22日", "申请表"],
        ["2027年12月31日", "2028年1月1日"],
        None,
    ),
    (
        "wechat",
        '<div id="js_content"><p>研究生请于10月23日前报名。</p><span style="display:none">2029年9月9日截止</span></div><footer>请关注其他公众号</footer>',
        ["研究生", "10月23日"],
        ["2029年9月9日", "其他公众号"],
        "ok",
    ),
    (
        "table",
        '<div class="v_news_content"><h2>本科生申请通知</h2><table><tr><td>材料</td><td>申请表、成绩单</td></tr><tr><td>学院提交</td><td>2026年10月24日</td></tr><tr><td>学校提交</td><td>2026年10月26日</td></tr></table></div>',
        ["申请表", "成绩单", "2026年10月24日", "2026年10月26日"],
        [],
        "ok",
    ),
    (
        "unknown",
        "<div><p>全体本科生于2026年10月25日前提交证明。</p></div><nav>购物推荐</nav>",
        ["2026年10月25日", "证明"],
        ["购物推荐"],
        "partial",
    ),
    (
        "application-form",
        '<main><h1>报名申请</h1><form><p>请携带身份证和申请表于10月26日到办公室办理。</p><input name="student_name"></form></main>',
        ["身份证", "申请表", "办公室"],
        [],
        None,
    ),
    (
        "captcha-word",
        "<article><h1>报名流程说明</h1><p>报名请输入验证码后提交申请表。截止时间为2026年10月27日。</p></article>",
        ["2026年10月27日", "验证码", "申请表"],
        [],
        "ok",
    ),
    (
        "header",
        "<header><nav>首页 校园新闻</nav></header><article><header><h1>奖学金材料补交通知</h1></header><p>10月28日前提交成绩单。</p></article>",
        ["奖学金材料补交通知", "成绩单"],
        ["校园新闻"],
        "ok",
    ),
    (
        "legitimate-class",
        '<article><section class="related-materials"><h2>相关材料</h2><p>附件包括申请表和导师推荐信。</p></section></article>',
        ["申请表", "导师推荐信"],
        [],
        "ok",
    ),
    (
        "hidden",
        '<article><p>请于10月29日前缴费。</p><div hidden>2030年1月1日截止</div><p aria-hidden="true">2031年2月2日截止</p></article>',
        ["10月29日"],
        ["2030年1月1日", "2031年2月2日"],
        "ok",
    ),
    (
        "contact",
        "<article><p>截止10月30日，提交申请表。</p><footer>承办单位联系方式：办公室022-12345678。</footer></article>",
        ["10月30日", "申请表", "022-12345678"],
        [],
        "ok",
    ),
    (
        "login",
        '<main><h1>统一身份认证</h1><form><label>学号</label><input name="user"><label>密码</label><input type="password"><button>登录</button></form></main>',
        [],
        ["2026年"],
        "failed",
    ),
    (
        "access-captcha",
        '<main><h1>访问验证</h1><p>请完成安全验证后继续访问</p><input name="captcha"><button>验证</button></main>',
        [],
        [],
        "failed",
    ),
]


@pytest.mark.parametrize("name,html,required,forbidden,state", CASES, ids=[row[0] for row in CASES])
def test_independent_body_layout(name, html, required, forbidden, state):
    segments, actual, warnings = html_body_segments(html.encode(), "https://fixture.example/notice")
    text = "\n".join(row["text"] for row in segments)
    for value in required:
        assert value in text
    for value in forbidden:
        assert value not in text
    if state:
        assert actual == state
