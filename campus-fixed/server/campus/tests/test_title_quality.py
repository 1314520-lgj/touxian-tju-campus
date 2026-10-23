"""Card titles have to be identifiable (t31).

A student decides whether to open a card from its title alone. The product already
refused *column-name* headlines (round 14, ``COLUMN_NAME_TITLES``), but three real
defects still reach the queue:

1. **Page furniture wins over the page's own headline.** ``me.tju.edu.cn`` prints
   「总体情况」 in a heading that sits *later* than the real ``<h1>``; the round-14
   heuristic treated any heading under ``<li>``/``<header>`` as a channel name, so the
   *real* headline was rejected and 「总体情况」 was chosen instead
   (``me-1041-53861.html`` / ``me-1041-52721.html``).
2. **A channel name that no list happens to contain.** ``sdem.tju.edu.cn`` prints
   「人事招聘」 as a nav heading; nothing rejects it, so a 推免生拟录取名单公示 got a
   card titled 「人事招聘」 (``sdem-1104-9600.html``).
3. **Nothing to fall back to.** Several columns (``scet``, ``cs``, ``cse/rczp``, ``yzb``)
   serve no usable ``<h1>`` at all; the listing page, however, already carries the real
   title. Using it is not invention — it is the same notice's own title, discovered
   earlier by the collector's listing parse.

These fixtures are the real pages the census fetched
(``verification/extraction/title_census.py``); every assertion below is about bytes a
student's card was actually built from.
"""

from __future__ import annotations

from pathlib import Path

from server.campus.content import (
    choose_card_title,
    html_body_segments,
    title_is_informative,
)

FIXTURES = Path(__file__).resolve().parents[2] / "verification" / "extraction" / "fixtures"


def card_title(fixture: str, url: str) -> str:
    segments, _state, _warnings = html_body_segments((FIXTURES / fixture).read_bytes(), url)
    return next((segment["text"] for segment in segments if segment["id"] == "title"), "")


# --- 1. the page's own headline must win over page furniture ----------------------

def test_real_me_notice_uses_its_own_headline():
    title = card_title("me-1041-53861.html", "https://me.tju.edu.cn/info/1041/53861.htm")
    assert title.startswith("机械工程学院2027年推荐优秀应届本科毕业生"), title
    assert title != "总体情况"


def test_real_me_camp_notice_uses_its_own_headline():
    title = card_title("me-1041-52721.html", "https://me.tju.edu.cn/info/1041/52721.htm")
    assert title.startswith("天津大学机械工程学院2026"), title
    assert "夏令营" in title


def test_real_sdem_notice_is_not_titled_after_a_nav_heading():
    title = card_title("sdem-1104-9600.html", "https://sdem.tju.edu.cn/info/1104/9600.htm")
    assert "推免生拟录取名单公示" in title, title
    assert title != "人事招聘"


# --- 2. the judgement itself, on real and on realistic titles ---------------------

def test_generic_and_channel_titles_are_not_informative():
    for title in ("会议通知", "通知", "公告", "公示", "总体情况", "下载中心", "人事招聘", "人才招聘",
                  "计算机学院", "化工学院", "天津大学研究生招生网", "更多", ""):
        assert not title_is_informative(title), title


def test_real_notice_titles_stay_informative():
    for title in (
        "机械工程学院2027年推荐优秀应届本科毕业生免试攻读硕士学位研究生和直接攻读博士学位研究生招生录取工作办法",
        "关于召开2027届毕业生就业工作推进会议的通知",
        "【2027博招】环境科学与工程学院2027级硕博连读研究生选拔的通知",
        "2026年下半年全国大学英语四、六级考试报名通知",
        "天津大学网络安全学院诚邀全球英才！",
    ):
        assert title_is_informative(title), title


# --- 3. the listing title is the fallback, and it is never invented --------------

def test_listing_title_replaces_a_channel_only_card():
    title, warning = choose_card_title("人事招聘", "天津大学卫生应急学院2027级推免生拟录取名单公示 2026-09-28",
                                       "天津大学卫生应急学院 · 通知公告")
    assert title == "天津大学卫生应急学院2027级推免生拟录取名单公示", title
    assert warning == "title_taken_from_listing"


def test_no_listing_title_no_invention():
    title, warning = choose_card_title("会议通知", "会议通知", "天津大学教务处 · 通知公告")
    assert title == "会议通知"  # kept as it is: nothing better exists, so nothing is made up
    assert warning == "title_unidentifiable"


def test_informative_page_title_is_untouched():
    real = "天津大学机械工程学院2026 年全国优秀大学生暑期夏令营活动通知"
    title, warning = choose_card_title(real, "列表页的另一条 2026-06-25", "天津大学机械工程学院 · 通知公告")
    assert title == real
    assert warning == ""


def test_channel_only_page_without_listing_title_is_marked_not_invented():
    title, warning = choose_card_title("人才招聘", "", "天津大学智能与计算学部 · 人才招聘")
    assert title == "人才招聘"
    assert warning == "title_unidentifiable"


# --- 4. a channel h1 in a real template shape ------------------------------------

def test_nav_heading_never_becomes_the_card_title():
    page = """
    <html><head><title>人才招聘-智能与计算学部</title></head><body>
      <div class="nav"><h1>人才招聘</h1></div>
      <div class="content"><p>天津大学智能与计算学部2027年公开招聘启事</p>
      <p>报名截止时间：2026年11月30日。请将材料发送至邮箱。</p></div>
    </body></html>
    """
    segments, _state, _warnings = html_body_segments(page.encode(), "https://cse.tju.edu.cn/info/1316/9999.htm")
    title = next((segment["text"] for segment in segments if segment["id"] == "title"), "")
    assert title != "人才招聘"