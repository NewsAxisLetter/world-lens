# -*- coding: utf-8 -*-
"""cluster.py のテスト（標準ライブラリ unittest のみ / C8）。

実行:
    python3 test_cluster.py
    python3 -m unittest -v test_cluster

フィクスチャは fixtures_cluster/ を使う。ネットワークには一切触らない。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures_cluster")
DATE = "2026-09-15"
EMPTY_DATE = "2026-09-16"

sys.path.insert(0, HERE)
import cluster  # noqa: E402

# 本文系キー（C1・A1）。topics/D.json に絶対に出してはいけない。
FORBIDDEN_KEYS = (
    "body", "content", "content_html", "content_encoded", "full_text",
    "description", "summary", "summary_original", "raw", "text", "html",
)


def run_cluster(out_dir, data_dir=FIX, date=DATE, extra=None):
    """cluster.py をサブプロセスで実行して出力 dict を返す。"""
    cmd = [sys.executable, os.path.join(HERE, "cluster.py"),
           "--date", date, "--data-dir", data_dir, "--out-dir", out_dir]
    if extra:
        cmd.extend(extra)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise AssertionError("cluster.py failed: %s"
                             % proc.stderr.decode("utf-8", "replace"))
    path = os.path.join(out_dir, "topics", "%s.json" % date)
    with open(path, encoding="utf-8") as f:
        return json.load(f), proc.stdout.decode("utf-8", "replace")


def walk_keys(obj, path=""):
    """入れ子構造の全キーを (パス, キー) で列挙する。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k
            for item in walk_keys(v, path + "/" + k):
                yield item
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            for item in walk_keys(v, "%s[%d]" % (path, i)):
                yield item


class TempOutMixin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cluster_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)


# ----------------------------------------------------------------------
# トークナイザ（必須1: AI無し・標準ライブラリのみ）
# ----------------------------------------------------------------------
class TestTokenize(unittest.TestCase):

    def test_japanese_uses_char_bigrams(self):
        # 日本語は分かち書きが無いので文字bi-gram。
        toks = cluster.tokenize("黒海で攻撃", "ja")
        self.assertIn("黒海", toks)
        self.assertIn("海で", toks)

    def test_chinese_uses_char_bigrams(self):
        toks = cluster.tokenize("黑海遭袭", "zh")
        self.assertIn("黑海", toks)

    def test_space_language_lowercased(self):
        # 空白区切り言語は語をlower化して比較する。
        a = cluster.tokenize("Black Sea GRAIN ship", "en")
        b = cluster.tokenize("black sea grain SHIP", "en")
        self.assertEqual(a, b)

    def test_cjk_and_latin_mixed(self):
        # 日本語文中のラテン語(NATO等)は語として拾えること。
        toks = cluster.tokenize("NATOが黒海を警戒", "ja")
        self.assertIn("nato", toks)

    def test_stopwords_removed_for_english(self):
        toks = cluster.tokenize("the ship in the sea", "en")
        self.assertNotIn("the", toks)
        self.assertNotIn("in", toks)

    def test_empty_text_is_empty_set(self):
        self.assertEqual(cluster.tokenize("", "en"), set())

    def test_tokenize_is_deterministic(self):
        # 冪等性(A2)の土台。同じ入力→同じトークン集合。
        for _ in range(3):
            self.assertEqual(cluster.tokenize("黒海で穀物輸送船が攻撃", "ja"),
                             cluster.tokenize("黒海で穀物輸送船が攻撃", "ja"))


class TestJaccard(unittest.TestCase):

    def test_identical_sets_score_one(self):
        s = cluster.tokenize("grain ship attacked", "en")
        sim, _ = cluster.weighted_jaccard(s, s)
        self.assertEqual(sim, 1.0)

    def test_disjoint_sets_score_zero(self):
        a = cluster.tokenize("telescope exoplanet water", "en")
        b = cluster.tokenize("interest rates inflation", "en")
        sim, _ = cluster.weighted_jaccard(a, b)
        self.assertEqual(sim, 0.0)

    def test_empty_set_never_matches(self):
        sim, _ = cluster.weighted_jaccard(set(), cluster.tokenize("x y", "en"))
        self.assertEqual(sim, 0.0)

    def test_symmetric(self):
        a = cluster.tokenize("black sea grain corridor", "en")
        b = cluster.tokenize("grain corridor attack", "en")
        self.assertEqual(cluster.weighted_jaccard(a, b)[0],
                         cluster.weighted_jaccard(b, a)[0])


# ----------------------------------------------------------------------
# 必須3: score = len(countries)*3 + media_count （A4検算）
# ----------------------------------------------------------------------
class TestScore(TempOutMixin):

    def setUp(self):
        TempOutMixin.setUp(self)
        self.doc, _ = run_cluster(self.tmp)

    def test_score_formula_matches_every_topic(self):
        for t in self.doc["topics"]:
            expect = len(t["countries"]) * 3 + t["media_count"]
            self.assertEqual(t["score"], expect,
                             "%s score mismatch" % t["topic_id"])

    def test_score_parts_reproduce_the_score(self):
        # 画面に「8×3＋13＝37」と出すための計算過程。必ず検算が合うこと。
        for t in self.doc["topics"]:
            p = t["score_parts"]
            self.assertEqual(p["countries"], len(t["countries"]))
            self.assertEqual(p["media"], t["media_count"])
            self.assertEqual(p["countries_weight"], 3)
            self.assertEqual(
                p["countries"] * p["countries_weight"] + p["media"],
                t["score"], t["topic_id"])

    def test_score_parts_has_display_expression(self):
        t = self.doc["topics"][0]
        # 契約§4 の score_parts は countries / countries_weight / media の3キー。
        # 画面の「7×3＋21＝42」はこの3値からフロントで組める。
        self.assertEqual(
            sorted(t["score_parts"]),
            ["countries", "countries_weight", "media"])

    def test_media_count_is_distinct_sources(self):
        for t in self.doc["topics"]:
            self.assertEqual(t["media_count"],
                             len(set(a["source"] for a in t["articles"])))

    def test_countries_are_unique_and_sorted(self):
        for t in self.doc["topics"]:
            self.assertEqual(t["countries"], sorted(set(t["countries"])))


# ----------------------------------------------------------------------
# 必須4: 並び順 score降順 → media_count降順 → topic_id昇順 （A5）
# ----------------------------------------------------------------------
class TestOrdering(TempOutMixin):

    def setUp(self):
        TempOutMixin.setUp(self)
        self.doc, _ = run_cluster(self.tmp)

    def test_topics_sorted_by_contract_rule(self):
        keys = [(-t["score"], -t["media_count"], t["topic_id"])
                for t in self.doc["topics"]]
        self.assertEqual(keys, sorted(keys))

    def test_ties_break_by_media_then_id(self):
        # 同点グループ内で media_count降順・topic_id昇順になっていること。
        seen = {}
        for t in self.doc["topics"]:
            seen.setdefault(t["score"], []).append(t)
        checked = 0
        for score, group in seen.items():
            if len(group) < 2:
                continue
            checked += 1
            keys = [(-g["media_count"], g["topic_id"]) for g in group]
            self.assertEqual(keys, sorted(keys), "score=%s の同点順序" % score)
        self.assertGreater(checked, 0, "同点グループが無いとこの検査は無意味")

    def test_topic_ids_are_unique(self):
        ids = [t["topic_id"] for t in self.doc["topics"]]
        self.assertEqual(len(ids), len(set(ids)))


# ----------------------------------------------------------------------
# 必須5: jp_reported / silent_countries / jp_none_count
# ----------------------------------------------------------------------
class TestJapanAndSilence(TempOutMixin):

    def setUp(self):
        TempOutMixin.setUp(self)
        self.doc, _ = run_cluster(self.tmp)

    def test_jp_reported_matches_articles(self):
        for t in self.doc["topics"]:
            has_jp = any(a["country"] == "JP" for a in t["articles"])
            self.assertEqual(t["jp_reported"], has_jp, t["topic_id"])

    def test_silent_countries_are_targets_minus_mentioned(self):
        # silent_countries = 収集対象国 − 言及があった国
        # 対象国は feeds[] 由来なので、実際の入力から同じ関数で母集団を作る。
        day = cluster.load_json(os.path.join(FIX, "2026-09-15.json"))
        enr = cluster.load_json(os.path.join(FIX, "2026-09-15.enriched.json"))
        joined, _ = cluster.join_articles(day, enr)
        targets = cluster.target_countries(day, joined)
        for t in self.doc["topics"]:
            expect = sorted(set(targets) - set(t["countries"]))
            self.assertEqual(t["silent_countries"], expect, t["topic_id"])

    def test_silent_and_countries_never_overlap(self):
        for t in self.doc["topics"]:
            self.assertFalse(set(t["countries"]) & set(t["silent_countries"]))

    def test_jp_none_count_counts_topics_japan_missed(self):
        # ユーザーが最も価値を置く出力（日本が報じていない話題の数）。
        expect = sum(1 for t in self.doc["topics"] if not t["jp_reported"])
        self.assertEqual(self.doc["stats"]["jp_none_count"], expect)

    def test_jp_none_count_is_present_and_meaningful(self):
        self.assertIn("jp_none_count", self.doc["stats"])
        self.assertGreater(self.doc["stats"]["jp_none_count"], 0,
                           "フィクスチャに日本未報道の話題が必要")

    def test_japan_only_topic_is_kept(self):
        # 必須10: 1記事だけの話題も捨てない（日本だけが報じた話題＝右カラム）
        jp_only = [t for t in self.doc["topics"]
                   if t["countries"] == ["JP"]]
        self.assertTrue(jp_only, "日本単独の話題が残っていない")

    def test_singleton_clusters_survive(self):
        singles = [t for t in self.doc["topics"] if len(t["articles"]) == 1]
        self.assertTrue(singles, "1記事クラスタが捨てられている")


# ----------------------------------------------------------------------
# 必須6: phrases（呼称の差の並置表）
# ----------------------------------------------------------------------
class TestPhrases(TempOutMixin):

    def setUp(self):
        TempOutMixin.setUp(self)
        self.doc, _ = run_cluster(self.tmp)
        self.top = self.doc["topics"][0]

    def test_original_is_preserved_verbatim(self):
        # C5: 原語は無加工。記事の title_original / key_phrase_original と
        # 完全一致する文字列であること（翻訳・整形してはいけない）。
        day = cluster.load_json(os.path.join(FIX, "%s.json" % DATE))
        raw = set()
        for a in day["articles"]:
            raw.add(a["title_original"])
        enr = cluster.load_json(os.path.join(FIX, "%s.enriched.json" % DATE))
        for e in enr["articles"]:
            if e.get("key_phrase_original"):
                raw.add(e["key_phrase_original"])
        for t in self.doc["topics"]:
            for p in t["phrases"]:
                self.assertIn(p["original"], raw,
                              "原語が加工されている: %r" % p["original"])

    def test_every_phrase_has_url(self):
        # C4: url 必須
        for t in self.doc["topics"]:
            for p in t["phrases"]:
                self.assertTrue(p["url"].startswith("http"), p)

    def test_phrase_has_required_fields(self):
        for p in self.top["phrases"]:
            # 契約§4 の phrases は媒体名を media キーで持つ（source ではない）。
            for k in ("country", "media", "media_type", "lang", "dir",
                      "stance", "stance_reason", "original", "ja", "url"):
                self.assertIn(k, p)

    def test_multi_country_topic_has_diverse_media_type(self):
        # 全部 state メディアだけにならないこと。
        types = set(p["media_type"] for p in self.top["phrases"])
        self.assertGreater(len(types), 1,
                           "media_type が単一: %s" % types)

    def test_multi_country_topic_has_diverse_stance(self):
        # 全部 neutral だけにならないこと。
        stances = set(p["stance"] for p in self.top["phrases"])
        self.assertGreater(len(stances), 1,
                           "stance が単一: %s" % stances)

    def test_phrases_one_row_per_medium(self):
        # 契約§4 の例では RU/TASS のように「媒体」単位で並べる。
        # 同じ国から複数媒体が出るのは正当（国内の誰呼の差も見せたい）。
        # 禁止すべきは同一媒体の重複。
        for t in self.doc["topics"]:
            media = [(p["country"], p["media"]) for p in t["phrases"]]
            self.assertEqual(len(media), len(set(media)), t["topic_id"])

    def test_phrases_capped(self):
        for t in self.doc["topics"]:
            self.assertLessEqual(len(t["phrases"]), cluster.MAX_PHRASES)


# ----------------------------------------------------------------------
# 必須7: facts_ja は summary_ja から / 空なら捏造しない
# ----------------------------------------------------------------------
class TestFactsJa(TempOutMixin):

    def test_facts_come_from_summary_ja_and_max_three(self):
        doc, _ = run_cluster(tempfile.mkdtemp(dir=self.tmp))
        enr = cluster.load_json(os.path.join(FIX, "%s.enriched.json" % DATE))
        pool = set()
        for e in enr["articles"]:
            if e.get("summary_ja"):
                pool.add(e["summary_ja"].strip())
        for t in doc["topics"]:
            self.assertLessEqual(len(t["facts_ja"]), 3)
            for line in t["facts_ja"]:
                self.assertIn(line, pool, "summary_ja に無い行: %r" % line)

    def test_degraded_gives_empty_facts_and_no_fabrication(self):
        # summary_ja が空（degraded）なら facts_ja は空配列。作文しない。
        data_dir = os.path.join(self.tmp, "dg")
        os.makedirs(data_dir)
        shutil.copy(os.path.join(FIX, "%s.json" % DATE),
                    os.path.join(data_dir, "%s.json" % DATE))
        shutil.copy(os.path.join(FIX, "%s.degraded.enriched.json" % DATE),
                    os.path.join(data_dir, "%s.enriched.json" % DATE))
        doc, _ = run_cluster(os.path.join(self.tmp, "out"), data_dir=data_dir)
        for t in doc["topics"]:
            self.assertEqual(t["facts_ja"], [], t["topic_id"])


# ----------------------------------------------------------------------
# 必須8: degraded は仕様。原語同士しか寄らずクラスタ数が増える。
# ----------------------------------------------------------------------
class TestDegraded(TempOutMixin):

    def setUp(self):
        TempOutMixin.setUp(self)
        data_dir = os.path.join(self.tmp, "dg")
        os.makedirs(data_dir)
        shutil.copy(os.path.join(FIX, "%s.json" % DATE),
                    os.path.join(data_dir, "%s.json" % DATE))
        shutil.copy(os.path.join(FIX, "%s.degraded.enriched.json" % DATE),
                    os.path.join(data_dir, "%s.enriched.json" % DATE))
        self.degraded, _ = run_cluster(os.path.join(self.tmp, "out1"),
                                       data_dir=data_dir)
        self.normal, _ = run_cluster(os.path.join(self.tmp, "out2"))

    def test_degraded_flag_is_true(self):
        self.assertTrue(self.degraded["degraded"])

    def test_normal_run_is_not_degraded(self):
        self.assertFalse(self.normal["degraded"])

    def test_degraded_produces_more_clusters(self):
        # 未翻訳だと言語を越えて寄れないのでクラスタ数が増える（仕様）。
        self.assertGreater(len(self.degraded["topics"]),
                           len(self.normal["topics"]))

    def test_degraded_same_article_count(self):
        # 記事が減るわけではない。束ね方だけが変わる。
        self.assertEqual(self.degraded["stats"]["articles"],
                         self.normal["stats"]["articles"])


# ----------------------------------------------------------------------
# 必須9: 本文系キーを出さない（C1・A1）／冪等（A2）
# ----------------------------------------------------------------------
class TestContractSafety(TempOutMixin):

    def setUp(self):
        TempOutMixin.setUp(self)
        self.doc, _ = run_cluster(self.tmp)

    def test_no_body_like_keys_anywhere(self):
        hits = [(p, k) for p, k in walk_keys(self.doc) if k in FORBIDDEN_KEYS]
        self.assertEqual(hits, [], "本文系キーが出力に含まれている: %s" % hits)

    def test_articles_expose_only_allowed_keys(self):
        allowed = {"article_id", "country", "source", "feed_id", "media_type",
                   "lang", "title_original", "title_ja", "url",
                   "published_at", "stance", "tags"}
        for t in self.doc["topics"]:
            for a in t["articles"]:
                extra = set(a) - allowed
                self.assertFalse(extra, "想定外キー: %s" % extra)

    def test_every_article_has_url(self):
        # C4: url 必須
        for t in self.doc["topics"]:
            for a in t["articles"]:
                self.assertTrue(a["url"].startswith("http"), a)

    def test_articles_without_url_are_dropped(self):
        # フィクスチャに url 欠落の記事を1件入れてある。
        self.assertEqual(self.doc["stats"]["dropped_no_url"], 1)

    def test_output_is_idempotent(self):
        # A2: 同じ入力で2回実行 → topic_id・並び順・件数が完全一致
        a_dir = os.path.join(self.tmp, "a")
        b_dir = os.path.join(self.tmp, "b")
        doc_a, _ = run_cluster(a_dir)
        doc_b, _ = run_cluster(b_dir)
        self.assertEqual([t["topic_id"] for t in doc_a["topics"]],
                         [t["topic_id"] for t in doc_b["topics"]])
        self.assertEqual(len(doc_a["topics"]), len(doc_b["topics"]))
        # generated_at 以外は完全一致すること。
        doc_a.pop("generated_at", None)
        doc_b.pop("generated_at", None)
        self.assertEqual(json.dumps(doc_a, sort_keys=True, ensure_ascii=False),
                         json.dumps(doc_b, sort_keys=True, ensure_ascii=False))

    def test_file_bytes_are_identical_across_runs(self):
        # A2の冒等性は「topic_id・並び順・件数が一致」。generated_at は実行時刻なので
        # 必ず差分が出る（差分が出ないといつ生成したか分からなくなる）。
        # そこで generated_at の行だけを除いてバイト比較する。
        p1 = os.path.join(self.tmp, "x")
        p2 = os.path.join(self.tmp, "y")
        run_cluster(p1)
        run_cluster(p2)
        f1 = os.path.join(p1, "topics", "%s.json" % DATE)
        f2 = os.path.join(p2, "topics", "%s.json" % DATE)

        def lines_without_timestamp(path):
            with open(path, encoding="utf-8") as fh:
                return [ln for ln in fh if "\"generated_at\"" not in ln]

        self.assertEqual(lines_without_timestamp(f1),
                         lines_without_timestamp(f2))

    def test_score_formula_string_is_the_contract_one(self):
        # C6: この式以外を使わない。
        self.assertEqual(self.doc["score_formula"],
                         "報道した国の数 × 3 ＋ 掲載媒体の数 × 1")

    def test_schema_version_present(self):
        self.assertEqual(self.doc["schema_version"], 3)

    def test_top_level_keys(self):
        for k in ("date", "generated_at", "schema_version", "degraded",
                  "score_formula", "stats", "alerts", "topics"):
            self.assertIn(k, self.doc)


# ----------------------------------------------------------------------
# クラスタリングの中身（多言語・同一話題が複数国にまたがる）
# ----------------------------------------------------------------------
class TestClusteringBehaviour(TempOutMixin):

    def setUp(self):
        TempOutMixin.setUp(self)
        self.doc, _ = run_cluster(self.tmp)

    def test_top_topic_spans_many_countries(self):
        # 黒海の話題は多言語（en/uk/ru/fr/ja/de/zh）で複数国にまたがる。
        top = self.doc["topics"][0]
        self.assertGreaterEqual(len(top["countries"]), 6)

    def test_top_topic_crosses_scripts(self):
        # ラテン・キリル・日本語・中国語が同じ話題に入ること。
        # articles[] に lang は無い（契約§4）ので、phrases[] の lang で見る。
        top = self.doc["topics"][0]
        langs = set(p["lang"] for p in top["phrases"])
        for expect in ("en", "ru", "ja", "zh"):
            self.assertIn(expect, langs, "lang=%s が寄っていない" % expect)

    def test_near_miss_decoys_stay_separate(self):
        # 「黒海の観光」「記録的な穀物収穫」は語が重なるが別の話題。
        # 同じクラスタに落ちていないこと（閾値が効いている証拠）。
        def topic_of(fragment):
            for t in self.doc["topics"]:
                for a in t["articles"]:
                    if fragment in a["title_original"]:
                        return t["topic_id"]
            self.fail("記事が見つからない: %s" % fragment)
        attack = topic_of("Grain ship attacked in Black Sea")
        tourism = topic_of("Black Sea tourism season")
        harvest = topic_of("Record grain harvest")
        port = topic_of("Odesa port expansion")
        self.assertNotEqual(attack, tourism)
        self.assertNotEqual(attack, harvest)
        self.assertNotEqual(attack, port)

    def test_unrelated_topics_not_merged(self):
        ids = set()
        for frag in ("Telescope spots water vapour",
                     "Central bank raises interest rates, inflation",
                     "Cúpula do clima"):
            for t in self.doc["topics"]:
                if any(frag in a["title_original"] for a in t["articles"]):
                    ids.add(t["topic_id"])
        self.assertEqual(len(ids), 3)

    def test_every_article_belongs_to_exactly_one_topic(self):
        seen = []
        for t in self.doc["topics"]:
            seen.extend(a["article_id"] for a in t["articles"])
        self.assertEqual(len(seen), len(set(seen)), "記事が複数話題に重複")
        self.assertEqual(len(seen), self.doc["stats"]["articles"])

    def test_stats_topics_matches_list_length(self):
        self.assertEqual(self.doc["stats"]["topics"], len(self.doc["topics"]))

    def test_threshold_recorded_in_stats(self):
        self.assertEqual(self.doc["stats"]["jaccard_threshold"],
                         cluster.JACCARD_THRESHOLD)

    def test_raising_threshold_increases_cluster_count(self):
        # 閾値を上げると寄りにくくなる → クラスタ数は減らない（単調性）。
        loose, _ = run_cluster(os.path.join(self.tmp, "lo"),
                               extra=["--threshold", "0.10"])
        tight, _ = run_cluster(os.path.join(self.tmp, "hi"),
                               extra=["--threshold", "0.50"])
        self.assertGreater(len(tight["topics"]), len(loose["topics"]))

    def test_articles_within_topic_sorted_stably(self):
        # 表示順が実行ごとにぶれないこと。契約は記事内の順を規定していないが、
        # 実装は「発行日時→article_id」の安定キーで並べている（古い順＝経緯が読める）。
        for t in self.doc["topics"]:
            keys = [(a["published_at"] or "9999", a["article_id"])
                    for a in t["articles"]]
            self.assertEqual(keys, sorted(keys), t["topic_id"])


class TestEmptyDay(TempOutMixin):

    def test_empty_day_produces_valid_empty_output(self):
        doc, _ = run_cluster(self.tmp, date=EMPTY_DATE)
        self.assertEqual(doc["topics"], [])
        self.assertEqual(doc["stats"]["articles"], 0)
        self.assertEqual(doc["stats"]["jp_none_count"], 0)
        self.assertTrue(doc["degraded"], "全フィード空なら degraded")

    def test_empty_day_keeps_alerts(self):
        doc, _ = run_cluster(self.tmp, date=EMPTY_DATE)
        self.assertTrue(doc["alerts"])


class TestVerifyMode(TempOutMixin):

    def test_verify_reports_pass(self):
        _, out = run_cluster(self.tmp, extra=["--verify"])
        self.assertIn("PASS", out)
        self.assertIn("0 MISMATCH", out)
        self.assertNotIn("MISMATCH!", out)

    def test_verify_prints_expression_for_display(self):
        _, out = run_cluster(self.tmp, extra=["--verify"])
        self.assertIn("×3 +", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
