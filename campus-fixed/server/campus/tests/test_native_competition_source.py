"""A column that only links WeChat posts has a native sibling — use it.

The two 教务处 columns 学生竞赛 (cxsj/xsjs.htm) and 竞赛科创通知 (tzgg/js_kc.htm)
link nothing but mp.weixin.qq.com posts, which answer an unauthenticated fetch
with an anti-scraping challenge. Reporting "needs authorization" and stopping
there was wrong: the same competition notices are published natively by the
新工科教育创新中心 (eee.tju.edu.cn), whose secretariat sits inside 教务处.

These tests pin the two facts that make the replacement legitimate:
  * the registered source list includes the native column;
  * its identity check still requires the declared origin, so it cannot be
    repointed at a WeChat host.
"""

from server.campus.sources import builtin_sources


def test_the_native_competition_column_is_a_registered_source():
    sources = builtin_sources()
    urls = {source["origin_url"] for source in sources}
    assert "https://eee.tju.edu.cn/tztg/tz.htm" in urls, sorted(urls)


def test_the_native_column_is_a_website_not_a_feed():
    source = next(s for s in builtin_sources() if s["origin_url"] == "https://eee.tju.edu.cn/tztg/tz.htm")
    assert source["adapter"] == "website"
    assert source["kind"] == "website"


def test_the_native_column_declares_its_own_article_pattern():
    """A source may only accept article URLs under its own declared origin."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / "config" / "campus-sources.json").read_text(encoding="utf-8"))
    entry = next(
        e for e in config["sources"] if e["origin_url"] == "https://eee.tju.edu.cn/tztg/tz.htm"
    )
    pattern = entry["config"]["article_url_pattern"]
    assert "eee\\.tju\\.edu\\.cn" in pattern
    assert "weixin" not in pattern
