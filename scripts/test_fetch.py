#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phase1/scripts/fetch.py のテスト。

    python3 test_fetch.py           # phase1/scripts で実行
    python3 -m unittest test_fetch  # どちらでも動く

ネットワークには一切出ない。testdata/ の固定XMLだけで判定する。

重点は4つ。
  A7  名前空間付きRSS(RDF)/Atom から記事が取れること
      → phase0 の findall('.//item') が朝日・DW・GOV.UK を0件にしたバグの回帰テスト
  C1/A1 出力に本文系キーが1つも無いこと（再帰検査）
  A2  同日2回実行で件数が増えないこと（article_id = sha256(url)[:8] による冪等）
  A8  1本が失敗しても残りの収集が続くこと
"""

import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TESTDATA = os.path.join(HERE, "testdata")
FEEDS_JSON = os.path.join(os.path.dirname(HERE), "feeds.json")

FETCHED_AT = "2026-09-14T12:00:00Z"
# 固定XMLの最新記事が 2026-09-14 なので、その少し後を「今」とする。
NOW = datetime(2026, 9, 14, 18, 0, 0, tzinfo=timezone.utc)


def read_testdata(name):
    with open(os.path.join(TESTDATA, name), "rb") as f:
        return f.read()


def make_feed(feed_id="t-feed", country="JP", source="テスト媒体",
              media_type="public", lang="ja", stale_after_days=2):
    """テスト用の feeds.json エントリ1件。"""
    return {
        "feed_id": feed_id,
        "source": source,
        "country": country,
        "media_type": media_type,
        "lang": lang,
        "rss_url": "https://example.test/%s.xml" % feed_id,
        "site_url": "https://example.test/",
        "kind": "news",
        "stale_after_days": stale_after_days,
        "max_items": 20,
        "enabled": True,
        "note": "",
    }


def walk_keys(node, path="$"):
    """入れ子のdict/listを再帰的に降りて (キー名, そこまでのパス) を全部返す。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, path + "." + str(k)
            for pair in walk_keys(v, path + "." + str(k)):
                yield pair
    elif isinstance(node, list):
        for i, v in enumerate(node):
            for pair in walk_keys(v, "%s[%d]" % (path, i)):
                yield pair


# ==================================================================
# A7：名前空間付きRSS(RDF)/Atom の回帰テスト
#
# phase0 は findall('.//item') を使っていたため、RSS 1.0(RDF) と Atom で
# 記事が1件も取れず、朝日・DW・GOV.UK が0件になった。
# ここでは「素朴な .//item は0件になる」ことを先に実証し、
# そのうえで fetch.py が正しく取れることを確かめる。
# ==================================================================

class TestNamespacedFeedsRegression(unittest.TestCase):

    def test_naive_dot_slash_item_really_fails_on_rdf(self):
        """まずバグを再現：findall('.//item') は RDF で0件になる。"""
        root = ET.fromstring(read_testdata("rdf_asahi.xml"))
        self.assertEqual(len(root.findall(".//item")), 0,
                         "この固定XMLは素朴な .//item が0件になる形でなければ"
                         "回帰テストとして意味がない")
        # 正しい実装は3件見つける
        self.assertEqual(len(fetch.find_entries(root)), 3)

    def test_naive_dot_slash_item_really_fails_on_atom(self):
        """Atom も同様に findall('.//item') では0件。"""
        root = ET.fromstring(read_testdata("atom_govuk.xml"))
        self.assertEqual(len(root.findall(".//item")), 0)
        self.assertEqual(len(fetch.find_entries(root)), 3)

    def test_rdf_asahi_yields_three_articles(self):
        """RSS 1.0 (RDF) 朝日型：3件取れて、見出しとURLが正しい。"""
        feed = make_feed("jp-asahi", "JP", "朝日新聞", "private", "ja")
        records = fetch.parse_items(read_testdata("rdf_asahi.xml"), feed, FETCHED_AT)
        self.assertEqual(len(records), 3, "RDFから3件取れていない（A7違反）")
        self.assertEqual(records[0]["title_original"],
                         "東京都大田区の工場で爆発か、3人けがの情報")
        self.assertEqual(records[0]["url"],
                         "https://www.asahi.com/articles/ASX1000001.html")
        self.assertEqual(records[0]["published_at"], "2026-09-14T01:09:17Z",
                         "dc:date のJSTをUTCに正規化できていない")
        self.assertEqual([r["rank_in_feed"] for r in records], [1, 2, 3])

    def test_atom_govuk_yields_three_entries(self):
        """Atom GOV.UK型：3件取れる。"""
        feed = make_feed("gb-govuk", "GB", "GOV.UK", "state", "en")
        records = fetch.parse_items(read_testdata("atom_govuk.xml"), feed, FETCHED_AT)
        self.assertEqual(len(records), 3, "Atomから3件取れていない（A7違反）")
        self.assertEqual(records[0]["title_original"],
                         "UK announces new sanctions package")

    def test_atom_link_picks_alternate_not_self_or_replies(self):
        """Atom の <link href> は rel="alternate" を選ぶ。

        先頭の <link> を素朴に拾うと rel="replies" のコメントURLを
        記事URLにしてしまう。1件目はその罠を仕込んである。
        """
        feed = make_feed("gb-govuk", "GB", "GOV.UK", "state", "en")
        records = fetch.parse_items(read_testdata("atom_govuk.xml"), feed, FETCHED_AT)
        self.assertEqual(records[0]["url"],
                         "https://www.gov.uk/government/news/uk-announces-new-sanctions")
        for r in records:
            self.assertNotIn("/comments/", r["url"], "rel=replies を拾っている")
            self.assertFalse(r["url"].endswith(".atom"),
                             "rel=self（フィード自身のURL）を記事URLにしている")

    def test_atom_falls_back_to_updated_when_published_missing(self):
        """<published> が無く <updated> だけの entry も日付が取れる。"""
        feed = make_feed("gb-govuk", "GB", "GOV.UK", "state", "en")
        records = fetch.parse_items(read_testdata("atom_govuk.xml"), feed, FETCHED_AT)
        self.assertEqual(records[2]["published_at"], "2026-09-12T10:00:00Z")

    def test_rss2_control_case_still_works(self):
        """対照実験：名前空間なしRSS2.0も同じコードパスで取れる。"""
        feed = make_feed("jp-nhk", "JP", "NHK", "public", "ja")
        records = fetch.parse_items(read_testdata("rss2_nhk.xml"), feed, FETCHED_AT)
        # 4項目あるが1件は <link> が空なので捨てられ、3件になる
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["published_at"], "2026-09-14T02:42:00Z")

    def test_item_without_pubdate_is_kept_with_null_published_at(self):
        """pubDate が無い記事も捨てない。published_at は None。"""
        feed = make_feed("jp-nhk", "JP", "NHK", "public", "ja")
        records = fetch.parse_items(read_testdata("rss2_nhk.xml"), feed, FETCHED_AT)
        self.assertIsNone(records[2]["published_at"])
        self.assertEqual(records[2]["title_original"],
                         "台風18号 週明けに本州へ接近の見込み")

    def test_item_without_link_is_dropped(self):
        """link が空の項目は捨てる（C4：URLは必須）。"""
        feed = make_feed("jp-nhk", "JP", "NHK", "public", "ja")
        records = fetch.parse_items(read_testdata("rss2_nhk.xml"), feed, FETCHED_AT)
        titles = [r["title_original"] for r in records]
        self.assertNotIn("リンクのない項目（捨てられるべき）", titles)
        for r in records:
            self.assertTrue(r["url"], "URLが空の記事が残っている（C4違反）")

    def test_all_three_formats_produce_identical_key_sets(self):
        """3形式すべてが同じキー集合を出す（形式差が下流に漏れない）。"""
        keysets = []
        for name in ("rss2_nhk.xml", "rdf_asahi.xml", "atom_govuk.xml"):
            records = fetch.parse_items(read_testdata(name), make_feed(), FETCHED_AT)
            for r in records:
                keysets.append(tuple(sorted(r.keys())))
        self.assertEqual(len(set(keysets)), 1, "形式によってキーが違う")
        self.assertEqual(set(keysets.pop()), set(fetch.ALLOWED_ARTICLE_FIELDS))


# ==================================================================
# C1 / A1：本文系キーを1つも出力しないこと（再帰検査）
# ==================================================================

class TestNoBodyFields(unittest.TestCase):

    def test_fixtures_actually_contain_body_text(self):
        """前提確認：固定XMLには本文相当のテキストが入っている。

        入っていなければ「漏れていない」ことを示せない。
        """
        for name in ("rss2_nhk.xml", "rdf_asahi.xml", "atom_govuk.xml"):
            raw = read_testdata(name).decode("utf-8")
            self.assertTrue(
                ("description" in raw) or ("summary" in raw) or ("content" in raw),
                "%s に本文系要素が無い" % name)

    def test_no_forbidden_key_in_parsed_records(self):
        """parse_items の出力に本文系キーが無い。"""
        for name in ("rss2_nhk.xml", "rdf_asahi.xml", "atom_govuk.xml"):
            records = fetch.parse_items(read_testdata(name), make_feed(), FETCHED_AT)
            for r in records:
                for key, path in walk_keys(r):
                    self.assertFalse(
                        fetch.is_forbidden_key(key),
                        "%s に本文系キー %r が出力された（C1違反）" % (path, key))

    def test_body_text_value_does_not_leak_into_any_value(self):
        """キー名だけでなく、値としても本文が混ざっていないこと。"""
        records = fetch.parse_items(read_testdata("rdf_asahi.xml"),
                                    make_feed(), FETCHED_AT)
        blob = json.dumps(records, ensure_ascii=False)
        self.assertNotIn("保存してはならない", blob,
                         "description の本文が値として漏れている（C1違反）")
        self.assertNotIn("content:encoded", blob)
        # 見出しは残っている（C5）
        self.assertIn("消費税減税の大綱", blob)

    def test_whitelist_is_exactly_eleven_keys(self):
        """ホワイトリストは契約§2の例と同じ11キー。

        契約本文は「13キー」と書いているが、§2のJSON例と
        下流（③クラスタ）のfixtureはいずれも11キー。
        実際に下流が読む形＝11キーに合わせる。数が変わったらここで気づける。
        """
        self.assertEqual(len(fetch.ALLOWED_ARTICLE_FIELDS), 11)
        self.assertEqual(set(fetch.ALLOWED_ARTICLE_FIELDS), {
            "article_id", "feed_id", "source", "country", "media_type", "lang",
            "title_original", "url", "published_at", "fetched_at", "rank_in_feed",
        })

    def test_no_forbidden_key_in_whitelist(self):
        """ホワイトリスト自体が本文系キーを含まない（自己矛盾の検出）。"""
        for key in fetch.ALLOWED_ARTICLE_FIELDS:
            self.assertFalse(fetch.is_forbidden_key(key), key)

    def test_is_forbidden_key_catches_naming_variants(self):
        """区切り・大文字・接尾辞を変えて逃げようとしても捕まる。"""
        for key in ("body", "Body", "BODY", "body_html", "bodyText",
                    "content", "Content", "content:encoded", "content_encoded",
                    "contentEncoded", "encoded", "description", "Description",
                    "summary", "summary_ja", "text", "fulltext", "full_text",
                    "abstract", "excerpt", "articleBody", "snippet", "teaser"):
            self.assertTrue(fetch.is_forbidden_key(key),
                            "%r を本文系キーとして検出できていない" % key)

    def test_is_forbidden_key_allows_legitimate_keys(self):
        """正当なキーを誤検出しない。"""
        for key in ("article_id", "feed_id", "source", "country", "media_type",
                    "lang", "title_original", "url", "published_at",
                    "fetched_at", "rank_in_feed", "status", "note", "articles",
                    "date", "schema_version", "generated_at"):
            self.assertFalse(fetch.is_forbidden_key(key),
                             "%r を誤って本文系キーと判定した" % key)

    def test_assert_no_body_fields_raises_on_injected_key(self):
        """検査関数が実際に例外を投げる（ザルでないことの確認）。"""
        bad = {"articles": [{"article_id": "x", "description": "本文"}]}
        with self.assertRaises(Exception):
            fetch.assert_no_body_fields(bad, "test")

    def test_assert_no_body_fields_finds_deeply_nested_key(self):
        """深い入れ子でも見つける（再帰検査であることの確認）。"""
        bad = {"a": [{"b": {"c": [{"d": {"content": "本文"}}]}}]}
        with self.assertRaises(Exception):
            fetch.assert_no_body_fields(bad, "test")

    def test_assert_no_body_fields_passes_on_clean_payload(self):
        """正常なペイロードは通す。"""
        records = fetch.parse_items(read_testdata("rss2_nhk.xml"),
                                    make_feed(), FETCHED_AT)
        summary = fetch.build_day_summary(records, [], "2026-09-14T12:00:00Z")
        fetch.assert_no_body_fields(summary, "test")   # 例外が出なければ合格


# ==================================================================
# 要件5 / A2：article_id = sha256(url)[:8] と冪等性
# ==================================================================

class TestArticleIdAndIdempotency(unittest.TestCase):

    def test_article_id_is_sha256_prefix8(self):
        import hashlib
        url = "https://www3.nhk.or.jp/news/html/20260914/k10014000001000.html"
        expected = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
        self.assertEqual(fetch.make_article_id(url), expected)
        self.assertEqual(len(fetch.make_article_id(url)), 8)

    def test_same_url_same_id(self):
        url = "https://example.test/a"
        self.assertEqual(fetch.make_article_id(url), fetch.make_article_id(url))

    def test_different_url_different_id(self):
        self.assertNotEqual(fetch.make_article_id("https://example.test/a"),
                            fetch.make_article_id("https://example.test/b"))

    def test_parsing_twice_gives_identical_ids(self):
        """同じXMLを2回読めば同じIDが出る。"""
        feed = make_feed()
        first = fetch.parse_items(read_testdata("rdf_asahi.xml"), feed, FETCHED_AT)
        second = fetch.parse_items(read_testdata("rdf_asahi.xml"), feed,
                                   "2026-09-14T18:00:00Z")   # 取得時刻だけ変える
        self.assertEqual([r["article_id"] for r in first],
                         [r["article_id"] for r in second])

    def test_merge_twice_does_not_increase_count(self):
        """A2の核心：同日2回マージしても件数が増えない。"""
        feed = make_feed()
        records = fetch.parse_items(read_testdata("rdf_asahi.xml"), feed, FETCHED_AT)
        once = fetch.merge_records([], records)
        twice = fetch.merge_records(once, records)
        self.assertEqual(len(once), 3)
        self.assertEqual(len(twice), 3, "同日2回実行で件数が増えた（A2違反）")

    def test_merge_keeps_first_fetched_at(self):
        """再実行で fetched_at を書き換えない（最初に見た時刻を保つ）。"""
        feed = make_feed()
        first = fetch.parse_items(read_testdata("rdf_asahi.xml"), feed,
                                  "2026-09-14T03:00:00Z")
        later = fetch.parse_items(read_testdata("rdf_asahi.xml"), feed,
                                  "2026-09-14T21:00:00Z")
        merged = fetch.merge_records(fetch.merge_records([], first), later)
        for r in merged:
            self.assertEqual(r["fetched_at"], "2026-09-14T03:00:00Z")

    def test_merge_adds_genuinely_new_articles(self):
        """新しい記事は増える（冪等が「何も増えない」ではないことの確認）。"""
        feed = make_feed()
        base = fetch.parse_items(read_testdata("rdf_asahi.xml"), feed, FETCHED_AT)
        extra = fetch.parse_items(read_testdata("rss2_nhk.xml"),
                                  make_feed("jp-nhk"), FETCHED_AT)
        merged = fetch.merge_records(fetch.merge_records([], base), extra)
        self.assertEqual(len(merged), 6)

    def test_merge_dedupes_same_url_from_different_feeds(self):
        """同じURLが別フィードから来ても1件（IDはURL由来なので衝突する）。"""
        a = dict(make_feed("feed-a"))
        rec_a = fetch.parse_items(read_testdata("rdf_asahi.xml"), a, FETCHED_AT)
        b = dict(make_feed("feed-b"))
        rec_b = fetch.parse_items(read_testdata("rdf_asahi.xml"), b, FETCHED_AT)
        merged = fetch.merge_records(fetch.merge_records([], rec_a), rec_b)
        self.assertEqual(len(merged), 3)
        ids = [r["article_id"] for r in merged]
        self.assertEqual(len(ids), len(set(ids)))


# ==================================================================
# 要件7：鮮度監視は「最新記事の日付」で行う（件数では凍結を検出できない）
# ==================================================================

class TestFreshnessByLatestDate(unittest.TestCase):

    def test_fresh_feed_is_ok(self):
        feed = make_feed(stale_after_days=2)
        records = fetch.parse_items(read_testdata("rss2_nhk.xml"), feed, FETCHED_AT)
        status = fetch.check_freshness(records, feed, now=NOW)
        self.assertEqual(status["status"], fetch.STATUS_OK)
        self.assertEqual(status["articles"], 3)
        self.assertEqual(status["latest_published_at"], "2026-09-14T02:42:00Z")

    def test_frozen_feed_is_stale_despite_having_articles(self):
        """要件7の核心：件数は3件あるのに1年半止まっているフィードを stale にする。

        VOA World / 人民網 / JPost旧URL の実測パターン。
        件数監視（articles > 0 なら ok）では絶対に検出できない。
        """
        feed = make_feed("de-dw", "DE", "DW", "public", "en", stale_after_days=7)
        records = fetch.parse_items(read_testdata("rdf_dw_stale.xml"), feed, FETCHED_AT)
        self.assertEqual(len(records), 3, "件数はある（だから件数監視では通ってしまう）")
        status = fetch.check_freshness(records, feed, now=NOW)
        self.assertEqual(status["status"], fetch.STATUS_STALE,
                         "件数があるフィードの更新停止を検出できていない（要件7違反）")
        self.assertGreater(status["stale_days"], 500)
        self.assertIn("件数は3件あるため", status["note"],
                      "noteに『件数はあるが古い』ことが書かれていない")

    def test_count_based_monitoring_would_have_passed(self):
        """対照：件数監視なら通ってしまうことを明示する。"""
        feed = make_feed(stale_after_days=7)
        records = fetch.parse_items(read_testdata("rdf_dw_stale.xml"), feed, FETCHED_AT)
        self.assertTrue(len(records) > 0)                     # 件数監視: 合格
        status = fetch.check_freshness(records, feed, now=NOW)
        self.assertNotEqual(status["status"], fetch.STATUS_OK)  # 日付監視: 不合格

    def test_empty_feed_is_empty_not_failed(self):
        """HTTP 200 で0件は empty（failed とは区別する）。"""
        feed = make_feed()
        status = fetch.check_freshness([], feed, now=NOW)
        self.assertEqual(status["status"], fetch.STATUS_EMPTY)
        self.assertEqual(status["articles"], 0)
        self.assertIsNone(status["latest_published_at"])

    def test_records_without_any_date_are_stale_not_ok(self):
        """日付が1つも読めないときは ok に丸めない。"""
        feed = make_feed()
        records = [{"article_id": "x", "published_at": None}]
        status = fetch.check_freshness(records, feed, now=NOW)
        self.assertEqual(status["status"], fetch.STATUS_STALE)

    def test_status_values_are_limited_to_four(self):
        """status は ok/stale/empty/failed の4値のみ。"""
        self.assertEqual(
            {fetch.STATUS_OK, fetch.STATUS_STALE,
             fetch.STATUS_EMPTY, fetch.STATUS_FAILED},
            {"ok", "stale", "empty", "failed"})

    def test_latest_published_ignores_none_and_picks_max(self):
        """最新判定は最大値（先頭ではない）。日付欠落は無視する。"""
        recs = [
            {"published_at": "2026-09-10T00:00:00Z"},
            {"published_at": None},
            {"published_at": "2026-09-14T00:00:00Z"},   # これが最新
            {"published_at": "2026-09-12T00:00:00Z"},
        ]
        self.assertEqual(fetch.latest_published(recs),
                         datetime(2026, 9, 14, tzinfo=timezone.utc))

    def test_failed_status_has_same_shape_as_check_freshness(self):
        """failed のレコードも同じキー集合（下流が分岐せずに読める）。"""
        feed = make_feed()
        ok = fetch.check_freshness(
            fetch.parse_items(read_testdata("rss2_nhk.xml"), feed, FETCHED_AT),
            feed, now=NOW)
        failed = fetch.failed_status(feed, "URLError: timed out")
        self.assertEqual(set(ok.keys()), set(failed.keys()))
        self.assertEqual(failed["status"], fetch.STATUS_FAILED)
        self.assertEqual(failed["articles"], 0)


# ==================================================================
# A8：1フィードが失敗・タイムアウトしても残りの収集が続くこと
# ==================================================================

class TestResilience(unittest.TestCase):

    def test_non_xml_body_becomes_failed_without_raising(self):
        """HTTP 200 だが中身がHTML（同意ページ）でも例外を外に出さない。

        実測で Le Monde が Cookie 同意ページを、Folha が HTML エラーページを
        200 で返した。
        """
        feed = make_feed("fr-lemonde", "FR", "Le Monde", "private", "fr")
        records, status = fetch.collect_feed(
            feed, FETCHED_AT, now=NOW, offline_dir=TESTDATA, log=lambda *a: None)
        # testdata に fr-lemonde.xml は無いので読み込み失敗 → failed
        self.assertEqual(records, [])
        self.assertEqual(status["status"], fetch.STATUS_FAILED)

    def test_parse_error_is_contained(self):
        """XMLとして壊れていても collect_feed は例外を投げない。"""
        feed = make_feed("broken", "JP", "壊れた媒体", "private", "ja")
        broken = os.path.join(TESTDATA, "broken.xml")
        with open(broken, "wb") as f:
            f.write(read_testdata("not_xml.html"))
        try:
            records, status = fetch.collect_feed(
                feed, FETCHED_AT, now=NOW, offline_dir=TESTDATA,
                log=lambda *a: None)
        finally:
            os.remove(broken)
        self.assertEqual(records, [])
        self.assertEqual(status["status"], fetch.STATUS_FAILED)
        self.assertIn("XML", status["note"])

    def test_one_dead_feed_does_not_stop_the_others(self):
        """A8の核心：3本のうち真ん中が死んでいても残り2本は取れる。"""
        feeds = [
            make_feed("rss2_nhk", "JP", "NHK", "public", "ja"),
            make_feed("missing-feed", "US", "存在しない媒体", "private", "en"),
            make_feed("rdf_asahi", "JP", "朝日新聞", "private", "ja"),
        ]
        records, statuses = fetch.collect_all(
            feeds, FETCHED_AT, now=NOW, offline_dir=TESTDATA,
            inter_feed_wait=0, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(len(statuses), 3, "状態レコードが3本分そろっていない")
        by_id = {s["feed_id"]: s for s in statuses}
        self.assertEqual(by_id["missing-feed"]["status"], fetch.STATUS_FAILED)
        self.assertEqual(by_id["rss2_nhk"]["articles"], 3)
        self.assertEqual(by_id["rdf_asahi"]["articles"], 3)
        self.assertEqual(len(records), 6,
                         "死んだ1本のせいで残りの収集が止まっている（A8違反）")

    def test_all_feeds_failing_still_returns_cleanly(self):
        """全滅しても例外を投げずに status:failed を返す。"""
        feeds = [make_feed("nope-1"), make_feed("nope-2")]
        records, statuses = fetch.collect_all(
            feeds, FETCHED_AT, now=NOW, offline_dir=TESTDATA,
            inter_feed_wait=0, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(records, [])
        self.assertTrue(all(s["status"] == fetch.STATUS_FAILED for s in statuses))
        summary = fetch.build_day_summary(records, statuses, "2026-09-14T12:00:00Z")
        self.assertEqual(summary["status"], fetch.STATUS_FAILED)

    def test_feed_statuses_are_returned_in_definition_order(self):
        """取得順はホスト分散で入れ替わるが、出力は feeds.json の定義順。"""
        feeds = [
            make_feed("rdf_asahi", "JP"),
            make_feed("rss2_nhk", "JP"),
            make_feed("atom_govuk", "GB"),
        ]
        _, statuses = fetch.collect_all(
            feeds, FETCHED_AT, now=NOW, offline_dir=TESTDATA,
            inter_feed_wait=0, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual([s["feed_id"] for s in statuses],
                         ["rdf_asahi", "rss2_nhk", "atom_govuk"])


# ==================================================================
# 要件9：レート配慮（同一ホスト連続アクセスを避ける／UA／タイムアウト）
# ==================================================================

class TestRateLimiting(unittest.TestCase):

    def test_interleave_avoids_consecutive_same_host(self):
        """同じホストが連続しない並びになる。"""
        feeds = []
        for i in range(3):
            f = make_feed("a%d" % i)
            f["rss_url"] = "https://same.example/%d.xml" % i
            feeds.append(f)
        for i in range(3):
            f = make_feed("b%d" % i)
            f["rss_url"] = "https://other.example/%d.xml" % i
            feeds.append(f)
        order = fetch.interleave_by_host(feeds)
        self.assertEqual(len(order), 6, "並べ替えでフィードが失われた")
        hosts = [f["rss_url"].split("/")[2] for f in order]
        for a, b in zip(hosts, hosts[1:]):
            self.assertNotEqual(a, b, "同一ホストが連続している: %s" % hosts)

    def test_interleave_preserves_every_feed(self):
        """並べ替えで重複・欠落が起きない。"""
        feeds = [make_feed("f%d" % i) for i in range(7)]
        order = fetch.interleave_by_host(feeds)
        self.assertEqual(sorted(f["feed_id"] for f in order),
                         sorted(f["feed_id"] for f in feeds))

    def test_user_agent_is_set_and_identifies_the_bot(self):
        """UAを名乗る（要件9）。"""
        self.assertTrue(fetch.USER_AGENT)
        self.assertNotIn("Python-urllib", fetch.USER_AGENT)
        self.assertIn("http", fetch.USER_AGENT.lower(),
                      "連絡先URLを含めるべき")

    def test_timeout_is_bounded(self):
        """タイムアウトが設定されている（無限待ちしない）。"""
        self.assertGreater(fetch.TIMEOUT_SEC, 0)
        self.assertLessEqual(fetch.TIMEOUT_SEC, 60)

    def test_throttle_waits_for_same_host_only(self):
        """同一ホストへの連続アクセスだけ待つ。別ホストは待たない。"""
        slept = []
        th = fetch.HostThrottle(min_interval=5.0,
                                sleep=slept.append,
                                clock=lambda: 0.0)   # 時間は進まない
        th.wait("https://a.example/1.xml")
        self.assertEqual(slept, [], "初回アクセスで待っている")
        th.wait("https://b.example/1.xml")
        self.assertEqual(slept, [], "別ホストで待っている")
        th.wait("https://a.example/2.xml")
        self.assertEqual(len(slept), 1, "同一ホスト2回目で待っていない")
        self.assertAlmostEqual(slept[0], 5.0, places=3)

    def test_inter_feed_wait_is_applied_between_feeds(self):
        """フィードの合間に待つ。最後の1本の後には待たない。"""
        slept = []
        feeds = [make_feed("rss2_nhk"), make_feed("rdf_asahi"), make_feed("atom_govuk")]
        fetch.collect_all(feeds, FETCHED_AT, now=NOW, offline_dir=TESTDATA,
                          inter_feed_wait=1.5, sleep=slept.append,
                          log=lambda *a: None)
        self.assertEqual(slept, [1.5, 1.5], "合間の待ち時間が正しくない")


# ==================================================================
# feeds.json の検証（契約§1 / A10：12カ国すべてに enabled:true が2本以上）
# ==================================================================

EXPECTED_COUNTRIES = ("JP", "US", "GB", "FR", "DE", "RU", "UA",
                      "CN", "KR", "IN", "BR", "QA")


class TestFeedsJson(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(FEEDS_JSON, encoding="utf-8") as f:
            cls.doc = json.load(f)
        cls.feeds = cls.doc["feeds"]
        cls.enabled = [f for f in cls.feeds if f["enabled"]]

    def test_loads_and_validates_through_fetch(self):
        """fetch.load_feeds が実ファイルを検証込みで読める。"""
        feeds = fetch.load_feeds(FEEDS_JSON)
        self.assertTrue(feeds)
        self.assertTrue(all(f["enabled"] for f in feeds),
                        "load_feeds が enabled:false を返している")

    def test_twelve_countries_each_have_two_or_more_enabled(self):
        """A10：12カ国すべてに enabled:true が2本以上。"""
        counts = {}
        for f in self.enabled:
            counts[f["country"]] = counts.get(f["country"], 0) + 1
        shortfall = {c: counts.get(c, 0) for c in EXPECTED_COUNTRIES
                     if counts.get(c, 0) < 2}
        self.assertEqual(shortfall, {},
                         "2媒体を下回る国がある（A10違反）: %s" % shortfall)

    def test_all_twelve_countries_present(self):
        self.assertEqual(sorted({f["country"] for f in self.enabled}),
                         sorted(EXPECTED_COUNTRIES))

    def test_dead_feeds_are_kept_but_disabled_with_reason(self):
        """死亡確認済み（Reuters/AP/VOA/人民網）は消さず disabled + 理由。"""
        by_id = {f["feed_id"]: f for f in self.feeds}
        dead = [fid for fid in by_id
                if any(k in fid for k in ("reuters", "ap-", "-ap", "voa", "people"))]
        self.assertTrue(dead, "死亡確認済みフィードが1本も残っていない")
        for fid in dead:
            f = by_id[fid]
            self.assertFalse(f["enabled"], "%s が enabled:true のまま" % fid)
            self.assertTrue(f["note"].strip(), "%s に理由(note)が無い" % fid)

    def test_no_enabled_feed_is_known_dead(self):
        """実測で死んでいたURLが enabled:true で残っていない。"""
        known_dead_fragments = (
            "feeds.reuters.com",
            "rsshub.app",
            "voanews.com/api/",
            "english.people.com.cn/rss",
        )
        for f in self.enabled:
            for frag in known_dead_fragments:
                self.assertNotIn(frag, f["rss_url"],
                                 "%s は死亡確認済みURLなのに enabled:true" % f["feed_id"])

    def test_every_feed_has_required_keys(self):
        required = ("feed_id", "source", "country", "media_type", "lang",
                    "rss_url", "site_url", "kind", "stale_after_days",
                    "max_items", "enabled", "note")
        for f in self.feeds:
            for key in required:
                self.assertIn(key, f, "%s に %s が無い" % (f.get("feed_id"), key))

    def test_feed_ids_are_unique(self):
        ids = [f["feed_id"] for f in self.feeds]
        self.assertEqual(len(ids), len(set(ids)), "feed_id が重複している")

    def test_rss_urls_are_unique(self):
        urls = [f["rss_url"] for f in self.enabled]
        self.assertEqual(len(urls), len(set(urls)), "rss_url が重複している")

    def test_media_type_values_are_valid(self):
        for f in self.feeds:
            self.assertIn(f["media_type"], fetch.VALID_MEDIA_TYPES,
                          "%s の media_type が不正: %s"
                          % (f["feed_id"], f["media_type"]))

    def test_country_codes_are_two_letter_upper(self):
        for f in self.feeds:
            self.assertRegex(f["country"], r"^[A-Z]{2}$")

    def test_urls_are_https_or_noted_http(self):
        """httpのままのフィードは note に理由を書く。"""
        for f in self.enabled:
            if f["rss_url"].startswith("http://"):
                self.assertTrue(f["note"].strip(),
                                "%s は http だが note が空" % f["feed_id"])

    def test_stale_after_days_is_positive_int(self):
        for f in self.feeds:
            self.assertIsInstance(f["stale_after_days"], int)
            self.assertGreater(f["stale_after_days"], 0)

    def test_each_country_has_media_type_diversity(self):
        """各国で media_type が1種類に偏っていないこと（論調比較の前提）。

        2本しか無い国は同種になりうるので、3本以上ある国だけ確認する。
        """
        by_country = {}
        for f in self.enabled:
            by_country.setdefault(f["country"], []).append(f["media_type"])
        for country, types in by_country.items():
            if len(types) >= 3:
                self.assertGreater(
                    len(set(types)), 1,
                    "%s は%d本すべて media_type=%s で偏っている"
                    % (country, len(types), types[0]))

    def test_notes_record_live_check_results(self):
        """全フィードの note に実測結果が書かれている（要件2）。"""
        for f in self.feeds:
            self.assertTrue(f["note"].strip(),
                            "%s の note が空（実測結果を書くこと）" % f["feed_id"])


# ==================================================================
# 出力（契約§2）：schema_version 3 の days/D.json
# ==================================================================

class TestDaySummary(unittest.TestCase):

    def setUp(self):
        self.feeds = [make_feed("rss2_nhk", "JP", "NHK", "public", "ja"),
                      make_feed("atom_govuk", "GB", "GOV.UK", "state", "en")]
        self.records, self.statuses = fetch.collect_all(
            self.feeds, FETCHED_AT, now=NOW, offline_dir=TESTDATA,
            inter_feed_wait=0, sleep=lambda s: None, log=lambda *a: None)
        self.summary = fetch.build_day_summary(
            self.records, self.statuses, "2026-09-14T12:00:00Z")

    def test_schema_version_is_three(self):
        self.assertEqual(self.summary["schema_version"], 3)

    def test_required_top_level_keys(self):
        for key in ("schema_version", "date", "generated_at",
                    "status", "feeds", "articles"):
            self.assertIn(key, self.summary)

    def test_articles_are_sorted_newest_first(self):
        dates = [a["published_at"] for a in self.summary["articles"]
                 if a["published_at"]]
        self.assertEqual(dates, sorted(dates, reverse=True),
                         "記事が新しい順に並んでいない")

    def test_status_ok_when_feeds_succeed(self):
        self.assertEqual(self.summary["status"], fetch.STATUS_OK)

    def test_output_is_json_serialisable_and_stable(self):
        """2回ダンプしても同じ文字列（キー順が安定＝git差分が読める）。"""
        a = json.dumps(self.summary, ensure_ascii=False, sort_keys=True, indent=2)
        b = json.dumps(self.summary, ensure_ascii=False, sort_keys=True, indent=2)
        self.assertEqual(a, b)

    def test_summary_contains_no_body_fields(self):
        """完成品まるごと再帰検査（A1）。"""
        fetch.assert_no_body_fields(self.summary, "day summary")
        for key, path in walk_keys(self.summary):
            self.assertFalse(fetch.is_forbidden_key(key),
                             "%s に本文系キー %r" % (path, key))

    def test_every_article_has_exactly_the_whitelisted_keys(self):
        for a in self.summary["articles"]:
            self.assertEqual(set(a.keys()), set(fetch.ALLOWED_ARTICLE_FIELDS))

    def test_date_format_is_iso_day(self):
        self.assertRegex(self.summary["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_generated_at_is_utc_z(self):
        self.assertTrue(self.summary["generated_at"].endswith("Z"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
