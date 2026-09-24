#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""②AI付与（enrich.py）のテスト。

HTTP層（enrich.http_post_json）を全面的に差し替えて、ネットワークもAPIキーも
使わずに検証する。実行方法:

    python3 phase1/scripts/test_enrich.py

課題で指定された4系統を最優先で確認する:
  1) 正常系        … AIの結果が正しく反映され degraded:false
  2) 429           … 指数バックオフ後、そのバッチだけ passthrough・他は継続
  3) 不正JSON      … その記事だけ passthrough・他の記事は生き残る
  4) キー無し      … 例外なく完走し degraded:true / enriched_by:passthrough
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import enrich  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as f:
        return json.load(f)


def gemini_envelope(results):
    """results 配列を Gemini のレスポンス形（candidates→parts→text）に包む。"""
    return {
        "candidates": [
            {"content": {"parts": [{"text": json.dumps({"results": results},
                                                       ensure_ascii=False)}]},
             "finishReason": "STOP"}
        ]
    }


def good_result(article_id, **over):
    out = {
        "id": article_id,
        "title_ja": "見出しの日本語訳",
        "summary_ja": "事実のみの1文目。事実のみの2文目。",
        "tags": ["安全保障"],
        "stance": "neutral",
        "stance_reason": "中立的な語彙を用いている",
        "key_phrase_original": "original phrase",
        "key_phrase_ja": "原語の訳",
    }
    out.update(over)
    return out


class FakeHTTP(object):
    """http_post_json の差し替え。呼ばれた回数と、都度返す応答を制御する。

    responses は 1リクエストごとに1要素を消費する。要素は
      - dict           … そのままボディとして返す
      - (status, body) … HTTPError 相当として enrich.ApiError を投げる
      - Exception      … そのまま投げる（URLError など）
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, api_key, timeout=None):
        self.calls.append({"url": url, "payload": payload, "api_key": api_key})
        if not self.responses:
            raise AssertionError("想定より多くHTTPリクエストが出ました（%d回目）"
                                 % len(self.calls))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            status, _body = item
            # 本物の http_post_json と同じ判定で retryable を立てる。
            # ここを本物と食い違わせるとリトライ挙動のテストが意味を失う。
            raise enrich.ApiError(
                "HTTP %s" % status, status=status,
                retryable=(status == 429 or 500 <= status < 600))
        return item


class EnrichTestCase(unittest.TestCase):
    """共通の下準備。sleep を無効化して、テストを実時間で待たせない。"""

    def setUp(self):
        self._real_sleep = enrich._sleep
        self.slept = []
        enrich._sleep = lambda s: self.slept.append(s)
        self._real_post = enrich.http_post_json

    def tearDown(self):
        enrich._sleep = self._real_sleep
        enrich.http_post_json = self._real_post

    def install(self, responses):
        fake = FakeHTTP(responses)
        enrich.http_post_json = fake
        return fake

    def by_id(self, payload):
        return {a["article_id"]: a for a in payload["articles"]}


# ==================================================================
# 系統1: 正常系
# ==================================================================

class TestHappyPath(EnrichTestCase):

    def test_ai_result_is_applied(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        fake = self.install([gemini_envelope([good_result(i) for i in ids])])

        out = enrich.enrich(day, date_key="2026-09-15", api_key="dummy-key",
                            batch_size=20)

        self.assertFalse(out["degraded"], "正常系で degraded が立ってはいけない")
        self.assertEqual(out["engine"], "gemini-2.5-flash")
        self.assertEqual(out["api_calls"], 1, "8記事は1リクエストで済むはず")
        self.assertEqual(len(fake.calls), 1)
        for art in out["articles"]:
            self.assertEqual(art["enriched_by"], "gemini")
            self.assertEqual(art["title_ja"], "見出しの日本語訳")
            self.assertTrue(art["summary_ja"])
            self.assertEqual(art["stance"], "neutral")
            self.assertTrue(art["stance_reason"])

    def test_output_shape_matches_contract(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        self.install([gemini_envelope([good_result(i) for i in ids])])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")

        # 契約§3のトップレベル必須キー
        for key in ("schema_version", "date", "generated_at", "engine",
                    "api_calls", "degraded", "articles"):
            self.assertIn(key, out, "トップレベルに %s が必要" % key)
        self.assertEqual(out["schema_version"], 3)
        self.assertEqual(out["date"], "2026-09-15")
        self.assertTrue(out["generated_at"].endswith("Z"))

        # 記事ごとの必須キーは契約§3 の9項目。§2の項目（url・country など）は
        # ここには入れない。③が article_id で D.json と突き合わせて復元する。
        need = ("article_id", "title_ja", "summary_ja", "tags", "stance",
                "stance_reason", "key_phrase_original", "key_phrase_ja",
                "enriched_by")
        for art in out["articles"]:
            self.assertEqual(sorted(art.keys()), sorted(need),
                             "契約§3 の9項目ちょうどであること（過不足なし）")

    def test_article_ids_match_the_input_so_phase3_can_join(self):
        """契約§4 は article_id で D.json と突き合わせる。IDの集合が一致すること。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        self.install([gemini_envelope([good_result(i) for i in ids])])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")

        self.assertEqual(sorted(a["article_id"] for a in out["articles"]),
                         sorted(ids), "①のIDと1対1で対応すること")

    def test_input_file_is_never_mutated(self):
        """入力オブジェクトを書き換えないこと（入力ファイル不変の担保）。"""
        day = load_fixture("day-multilang.json")
        before = json.dumps(day, ensure_ascii=False, sort_keys=True)
        ids = [a["article_id"] for a in day["articles"]]
        self.install([gemini_envelope([good_result(i) for i in ids])])
        enrich.enrich(day, date_key="2026-09-15", api_key="k")
        after = json.dumps(day, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after, "入力の dict を書き換えてはいけない")

    def test_request_uses_flash_model_and_header_key(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        fake = self.install([gemini_envelope([good_result(i) for i in ids])])
        enrich.enrich(day, date_key="2026-09-15", api_key="secret-value")

        call = fake.calls[0]
        self.assertIn("gemini-2.5-flash", call["url"])
        # キーはURLクエリではなくヘッダで送る（URLはログに残りやすい）
        self.assertNotIn("secret-value", call["url"])
        self.assertEqual(call["api_key"], "secret-value")


# ==================================================================
# 系統2: 429 / 5xx
# ==================================================================

class TestRetryAndRateLimit(EnrichTestCase):

    def test_429_then_success_is_retried(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        fake = self.install([
            (429, '{"error":{"message":"rate limit"}}'),
            (429, '{"error":{"message":"rate limit"}}'),
            gemini_envelope([good_result(i) for i in ids]),
        ])

        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")

        self.assertEqual(len(fake.calls), 3, "2回失敗し3回目で成功するはず")
        self.assertFalse(out["degraded"], "最終的に成功したので degraded は立たない")
        self.assertEqual(out["api_calls"], 3, "リトライも api_calls に数える")
        self.assertEqual(len(self.slept), 2, "リトライ前に2回待つはず")
        self.assertLess(self.slept[0], self.slept[1], "指数バックオフで待ち時間が伸びること")
        for art in out["articles"]:
            self.assertEqual(art["enriched_by"], "gemini")

    def test_429_exhausted_falls_back_to_passthrough(self):
        day = load_fixture("day-multilang.json")
        fake = self.install([(429, "rate limit")] * 4)

        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")

        self.assertEqual(len(fake.calls), 4, "初回1回 + リトライ最大3回 = 4回で打ち切る")
        self.assertTrue(out["degraded"], "全滅したので degraded:true（A3・C9）")
        self.assertEqual(out["engine"], "passthrough")
        for art in out["articles"]:
            self.assertEqual(art["enriched_by"], "passthrough")
            self.assertEqual(art["summary_ja"], "", "捏造せず空文字にする")

    def test_5xx_is_also_retried(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        fake = self.install([
            (503, "unavailable"),
            gemini_envelope([good_result(i) for i in ids]),
        ])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse(out["degraded"])

    def test_400_is_not_retried(self):
        """400（プロンプト不正など）はリトライしても直らないので即諦める。"""
        day = load_fixture("day-multilang.json")
        fake = self.install([(400, '{"error":{"message":"bad request"}}')])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        self.assertEqual(len(fake.calls), 1, "400 はリトライしない")
        self.assertTrue(out["degraded"])
        self.assertEqual(out["engine"], "passthrough")

    def test_invalid_key_401_completes_without_exception(self):
        """不正キー（401/403）でも例外で落ちず degraded で完走する（A9）。"""
        day = load_fixture("day-multilang.json")
        fake = self.install([(401, '{"error":{"message":"API key not valid"}}')])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="wrong-key")
        self.assertEqual(len(fake.calls), 1, "認証エラーはリトライ無駄なので1回")
        self.assertTrue(out["degraded"])
        self.assertEqual(out["engine"], "passthrough")
        self.assertEqual(len(out["articles"]), len(day["articles"]),
                         "記事は1件も失われないこと")

    def test_one_batch_fails_others_continue(self):
        """あるバッチが全滅しても、他のバッチの結果は捨てない。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        # batch_size=4 → 2バッチ。1バッチ目は4連敗、2バッチ目は成功。
        fake = self.install(
            [(500, "err")] * 4
            + [gemini_envelope([good_result(i) for i in ids[4:]])]
        )

        out = enrich.enrich(day, date_key="2026-09-15", api_key="k",
                            batch_size=4)

        self.assertEqual(len(fake.calls), 5)
        self.assertFalse(out["degraded"],
                         "半分はAIが効いているので全体 degraded にはしない")
        got = self.by_id(out)
        for aid in ids[:4]:
            self.assertEqual(got[aid]["enriched_by"], "passthrough")
            self.assertEqual(got[aid]["summary_ja"], "")
        for aid in ids[4:]:
            self.assertEqual(got[aid]["enriched_by"], "gemini")
            self.assertTrue(got[aid]["summary_ja"])
        self.assertTrue(out["notes"], "部分失敗は notes に残す（C7）")

    def test_network_error_falls_back_without_raising(self):
        import urllib.error
        day = load_fixture("day-multilang.json")
        self.install([urllib.error.URLError("dns failure")] * 4)
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        self.assertTrue(out["degraded"])
        self.assertEqual(len(out["articles"]), len(day["articles"]))

    def test_request_budget_is_capped(self):
        """上限に達したら残りは素通し。上限を超えてHTTPを出さない（A12）。"""
        day = load_fixture("day-multilang.json")
        # 1記事1バッチ＝8バッチ必要だが、上限3回しか出させない
        fake = self.install([gemini_envelope([good_result(a["article_id"])])
                             for a in day["articles"]])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k",
                            batch_size=1, max_requests=3)

        self.assertEqual(len(fake.calls), 3, "上限3回を超えてはいけない")
        self.assertEqual(out["api_calls"], 3)
        kinds = [a["enriched_by"] for a in out["articles"]]
        self.assertEqual(kinds.count("gemini"), 3)
        self.assertEqual(kinds.count("passthrough"), 5)
        self.assertEqual(len(out["articles"]), 8, "記事は落とさない")

    def test_default_batching_stays_within_daily_budget(self):
        """契約の想定最大件数でも1日30リクエストに収まることを数で確認。"""
        # 12カ国 × 各国最大4媒体 × 1媒体10件 = 480件を上限とみなす
        max_articles = 12 * 4 * 10
        batches = -(-max_articles // enrich.BATCH_SIZE)  # 切り上げ
        # バッチ数は MAX_BATCHES でも頭打ちされるので、小さい方が実際の回数。
        planned = min(batches, enrich.MAX_BATCHES)
        self.assertLessEqual(planned, 30, "A12: 1日30リクエスト以内")
        # リトライを含めた最悪値でも実測上限 250/日 に届かないこと。
        self.assertLessEqual(planned * (1 + enrich.MAX_RETRY), 250,
                             "全バッチが最大リトライしても 250/日 以内")


# ==================================================================
# 系統3: 不正JSON / 部分失敗
# ==================================================================

class TestMalformedResponse(EnrichTestCase):

    def test_completely_unparseable_text_degrades_batch_only(self):
        day = load_fixture("day-multilang.json")
        self.install([
            {"candidates": [{"content": {"parts": [{"text": "これはJSONではありません"}]}}]},
            {"candidates": [{"content": {"parts": [{"text": "またJSONではない"}]}}]},
        ])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k",
                            batch_size=4)
        # パース不能は「内容の問題」なのでリトライせず素通しへ
        self.assertTrue(out["degraded"])
        for art in out["articles"]:
            self.assertEqual(art["enriched_by"], "passthrough")
            self.assertEqual(art["summary_ja"], "")

    def test_json_wrapped_in_markdown_fence_is_recovered(self):
        """```json ... ``` で囲まれて返ってくる実挙動に耐えること。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        inner = json.dumps({"results": [good_result(i) for i in ids]},
                           ensure_ascii=False)
        self.install([{"candidates": [{"content": {"parts": [
            {"text": "```json\n" + inner + "\n```"}]}}]}])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        self.assertFalse(out["degraded"])
        for art in out["articles"]:
            self.assertEqual(art["enriched_by"], "gemini")

    def test_bare_array_response_is_accepted(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        inner = json.dumps([good_result(i) for i in ids], ensure_ascii=False)
        self.install([{"candidates": [{"content": {"parts": [{"text": inner}]}}]}])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        self.assertFalse(out["degraded"])

    def test_missing_article_in_response_is_passthrough_alone(self):
        """一部の記事だけ返ってこない場合、その記事のみ素通しにする。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        # 先頭2件だけ返す
        self.install([gemini_envelope([good_result(ids[0]), good_result(ids[1])])])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")

        got = self.by_id(out)
        self.assertEqual(got[ids[0]]["enriched_by"], "gemini")
        self.assertEqual(got[ids[1]]["enriched_by"], "gemini")
        for aid in ids[2:]:
            self.assertEqual(got[aid]["enriched_by"], "passthrough")
            self.assertEqual(got[aid]["summary_ja"], "")
        self.assertFalse(out["degraded"], "2件は成功しているので全体 degraded ではない")

    def test_garbage_item_is_passthrough_alone(self):
        """1記事分の中身が壊れていても、他の記事は生き残る。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        results = [good_result(i) for i in ids]
        results[0] = {"id": ids[0], "title_ja": 12345, "tags": "not-a-list",
                      "stance": {"x": 1}}   # 型がでたらめ
        results[1] = {"id": ids[1]}          # title_ja が無い
        self.install([gemini_envelope(results)])

        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        got = self.by_id(out)

        self.assertEqual(got[ids[0]]["enriched_by"], "passthrough")
        self.assertEqual(got[ids[1]]["enriched_by"], "passthrough")
        for aid in ids[2:]:
            self.assertEqual(got[aid]["enriched_by"], "gemini")
        self.assertEqual(len(out["articles"]), len(day["articles"]))

    def test_empty_candidates_is_handled(self):
        day = load_fixture("day-multilang.json")
        self.install([{"candidates": []}, {"promptFeedback": {"blockReason": "SAFETY"}}])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k", batch_size=4)
        self.assertTrue(out["degraded"])
        self.assertEqual(len(out["articles"]), len(day["articles"]))

    def test_canned_fixture_response_parses(self):
        """自作フィクスチャ（実レスポンス形）が読めること。"""
        raw = load_fixture("gemini-response-ok.json")
        by_id = enrich.parse_results(enrich.extract_text(raw))
        self.assertIn("c3d4e5f6", by_id)
        self.assertIn("e5f6a7b8", by_id)
        self.assertEqual(by_id["e5f6a7b8"]["stance"], "critical")


# ==================================================================
# 系統4: キー無し（最重要）
# ==================================================================

class TestNoApiKey(EnrichTestCase):

    def test_no_key_completes_with_degraded(self):
        day = load_fixture("day-multilang.json")
        # HTTPが呼ばれたら即失敗させる。1回も呼ばれないのが正しい。
        self.install([])

        out = enrich.enrich(day, date_key="2026-09-15", api_key="")

        self.assertTrue(out["degraded"], "C9: キー無しは degraded:true")
        self.assertEqual(out["engine"], "passthrough")
        self.assertEqual(out["api_calls"], 0, "キー無しでHTTPを出してはいけない")
        self.assertEqual(len(out["articles"]), len(day["articles"]))
        for art in out["articles"]:
            self.assertEqual(art["enriched_by"], "passthrough")
        self.assertTrue(out["notes"], "理由を notes に残す（C7）")

    def test_no_key_does_not_fabricate_summary(self):
        """A3: summary_ja は空文字。原語見出しの流用や作文をしない。"""
        day = load_fixture("day-multilang.json")
        self.install([])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="")
        for art in out["articles"]:
            self.assertEqual(art["summary_ja"], "", "要約を捏造してはいけない")
            self.assertEqual(art["tags"], [])
            self.assertEqual(art["stance"], "neutral", "判定不能は neutral")
            self.assertTrue(art["stance_reason"], "根拠文は必ず入れる")

    def test_no_key_falls_back_title_ja_to_original(self):
        """title_ja は原語見出しをそのまま入れる（画面を空にしないため）。

        訳したフリはしないので summary_ja とは扱いを分ける。
        """
        day = load_fixture("day-multilang.json")
        self.install([])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="")
        src = {a["article_id"]: a["title_original"] for a in day["articles"]}
        for art in out["articles"]:
            original = src[art["article_id"]]
            self.assertEqual(art["title_ja"], original)
            # C5: 原語キーフレーズは訳さず原語のまま
            self.assertEqual(art["key_phrase_original"], original)
            self.assertEqual(art["key_phrase_ja"], "")

    def test_env_var_is_the_only_key_source(self):
        saved = os.environ.pop("GEMINI_API_KEY", None)
        try:
            self.assertEqual(enrich.read_api_key(), "")
            os.environ["GEMINI_API_KEY"] = "  spaced-key  "
            self.assertEqual(enrich.read_api_key(), "spaced-key")
            # プレースホルダは未設定として扱う
            for bogus in ("", "   ", "your-api-key", "YOUR_API_KEY", "changeme", "None"):
                os.environ["GEMINI_API_KEY"] = bogus
                self.assertEqual(enrich.read_api_key(), "",
                                 "%r は未設定扱いにすべき" % bogus)
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
            if saved is not None:
                os.environ["GEMINI_API_KEY"] = saved

    def test_empty_day_completes(self):
        day = load_fixture("day-empty.json")
        self.install([])
        out = enrich.enrich(day, date_key="2026-09-16", api_key="")
        self.assertEqual(out["articles"], [])
        self.assertEqual(out["api_calls"], 0)
        self.assertTrue(out["degraded"])


# ==================================================================
# 契約の個別条項
# ==================================================================

class TestContractRules(EnrichTestCase):

    def test_tags_outside_the_fixed_set_are_dropped(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        results = [good_result(i) for i in ids]
        # 「芸能」「Politics」は固定12タグの外。「スポーツ」は集合内なので残る。
        results[0]["tags"] = ["安全保障", "芸能", "Politics", "外交"]
        results[1]["tags"] = ["ぜんぶ集合外", "unknown"]
        self.install([gemini_envelope(results)])

        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        got = self.by_id(out)

        self.assertEqual(got[ids[0]]["tags"], ["安全保障", "外交"])
        self.assertEqual(got[ids[1]]["tags"], [], "集合外だけなら空配列")
        for art in out["articles"]:
            for tag in art["tags"]:
                self.assertIn(tag, enrich.ALLOWED_TAGS)

    def test_allowed_tag_set_is_exactly_twelve(self):
        self.assertEqual(len(enrich.ALLOWED_TAGS), 12,
                         "契約§3の固定12タグであること")

    def test_tags_are_capped_and_deduped(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        results = [good_result(i) for i in ids]
        results[0]["tags"] = ["経済", "経済", "外交", "安全保障", "エネルギー", "環境"]
        self.install([gemini_envelope(results)])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        tags = self.by_id(out)[ids[0]]["tags"]
        self.assertEqual(tags, ["経済", "外交", "安全保障"], "重複を除き最大3つ")

    def test_invalid_stance_becomes_neutral(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        results = [good_result(i) for i in ids]
        results[0]["stance"] = "positive"       # 集合外
        results[1]["stance"] = "SUPPORT"        # 大文字は救う
        results[2]["stance"] = ""               # 空
        self.install([gemini_envelope(results)])

        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        got = self.by_id(out)

        self.assertEqual(got[ids[0]]["stance"], "neutral")
        self.assertEqual(got[ids[1]]["stance"], "support")
        self.assertEqual(got[ids[2]]["stance"], "neutral")
        for art in out["articles"]:
            self.assertIn(art["stance"], ("support", "critical", "neutral"))

    def test_stance_reason_is_always_present(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        results = [good_result(i) for i in ids]
        results[0].pop("stance_reason")
        results[1]["stance_reason"] = "   "
        self.install([gemini_envelope(results)])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        for art in out["articles"]:
            self.assertTrue(art["stance_reason"].strip(),
                            "stance には必ず根拠文を付ける")

    def test_summary_is_capped_at_two_sentences(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        results = [good_result(i) for i in ids]
        results[0]["summary_ja"] = "1文目。2文目。3文目。4文目。"
        self.install([gemini_envelope(results)])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        summary = self.by_id(out)[ids[0]]["summary_ja"]
        self.assertEqual(summary, "1文目。2文目。", "C2: 最大2文に切る")

    def test_key_phrase_original_keeps_source_language(self):
        """C5: 原語フレーズを日本語に潰さない。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        results = [good_result(i) for i in ids]
        # ロシア語の記事（index 2）にロシア語のフレーズを返す。
        # 見出しに実際に含まれる部分文字列を使う（含まれない語は見出しに差し戻される仕様）。
        ru_id = day["articles"][2]["article_id"]
        phrase = "Инцидент в акватории"
        self.assertIn(phrase, day["articles"][2]["title_original"])
        results[2]["key_phrase_original"] = phrase
        results[2]["key_phrase_ja"] = "海域での事案"
        self.install([gemini_envelope(results)])

        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        art = self.by_id(out)[ru_id]
        self.assertEqual(art["key_phrase_original"], phrase,
                         "原語のまま保持すること（日本語に潰さない）")
        self.assertEqual(art["key_phrase_ja"], "海域での事案")

    def test_key_phrase_original_falls_back_to_title_not_translation(self):
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        results = [good_result(i) for i in ids]
        results[0].pop("key_phrase_original")
        self.install([gemini_envelope(results)])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        art = self.by_id(out)[ids[0]]
        self.assertEqual(art["key_phrase_original"],
                         day["articles"][0]["title_original"],
                         "欠けたら原語見出しで代替する（訳文を入れない）")

    def test_no_body_text_anywhere(self):
        """C1・A1: 記事本文をプロンプトにも出力にも載せない。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        fake = self.install([gemini_envelope([good_result(i) for i in ids])])
        out = enrich.enrich(day, date_key="2026-09-15", api_key="k")

        banned = ("body", "content", "full_text", "description",
                  "summary_original", "article_body", "text")
        for art in out["articles"]:
            for key in art:
                self.assertNotIn(key, banned, "本文系キー %s を出力してはいけない" % key)

        # プロンプトに渡しているのは見出し等のメタのみであること
        sent = json.dumps(fake.calls[0]["payload"], ensure_ascii=False)
        for art in day["articles"]:
            self.assertIn(art["title_original"], sent)
        self.assertNotIn("full_text", sent)

    def test_assert_no_body_fields_raises_on_violation(self):
        bad = {"articles": [{"article_id": "x", "body": "本文が混入している"}]}
        with self.assertRaises(RuntimeError):
            enrich.assert_no_body_fields(bad, "enriched")

    def test_batching_groups_multiple_articles_per_request(self):
        """A12: 1記事1リクエストになっていないこと。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        fake = self.install([gemini_envelope([good_result(i) for i in ids])])
        enrich.enrich(day, date_key="2026-09-15", api_key="k")

        self.assertEqual(len(fake.calls), 1)
        sent = json.dumps(fake.calls[0]["payload"], ensure_ascii=False)
        # 8件すべてが1リクエストに同梱されている
        for art in day["articles"]:
            self.assertIn(art["title_original"], sent)

    def test_prompt_forbids_speculation(self):
        """C2: 評価・推測・形容の禁止をプロンプトで明示していること。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        fake = self.install([gemini_envelope([good_result(i) for i in ids])])
        enrich.enrich(day, date_key="2026-09-15", api_key="k")

        sent = json.dumps(fake.calls[0]["payload"], ensure_ascii=False)
        for word in ("事実", "推測", "禁止", "2文"):
            self.assertIn(word, sent, "プロンプトに「%s」の指示が必要" % word)

    def test_dirty_input_is_normalized(self):
        """url 無しは捨て、重複urlは1件に、article_id 欠けは補完する（C4）。"""
        day = load_fixture("day-dirty.json")
        self.install([])
        out = enrich.enrich(day, date_key="2026-09-17", api_key="")

        ids = [a["article_id"] for a in out["articles"]]
        self.assertEqual(len(ids), len(set(ids)), "article_id は重複しないこと")
        self.assertEqual(len(out["articles"]), 2,
                         "url無し1件を除外、重複1件を統合して2件")
        for art in out["articles"]:
            self.assertTrue(art["article_id"], "article_id は必ず埋める")
        # article_id が欠けていた記事は url の sha256 先頭8桁で補完される（§2の式）
        expect = hashlib.sha256(
            b"https://example.invalid/news/2").hexdigest()[:8]
        self.assertIn(expect, ids, "①と同じ式で article_id を作ること")

    def test_output_is_deterministic_for_same_input(self):
        """A11: 同じ入力を2回流しても同じ結果（generated_at 以外）。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]

        self.install([gemini_envelope([good_result(i) for i in ids])])
        first = enrich.enrich(day, date_key="2026-09-15", api_key="k")
        self.install([gemini_envelope([good_result(i) for i in ids])])
        second = enrich.enrich(day, date_key="2026-09-15", api_key="k")

        for payload in (first, second):
            payload.pop("generated_at")
        self.assertEqual(json.dumps(first, ensure_ascii=False, sort_keys=True),
                         json.dumps(second, ensure_ascii=False, sort_keys=True))


# ==================================================================
# CLI（ファイル入出力）
# ==================================================================

class TestCli(EnrichTestCase):

    def test_main_writes_output_and_exits_zero_without_key(self):
        day = load_fixture("day-multilang.json")
        self.install([])
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "in.json")
            out_path = os.path.join(tmp, "out.json")
            with open(in_path, "w", encoding="utf-8") as f:
                json.dump(day, f, ensure_ascii=False)
            with open(in_path, "rb") as f:
                before = f.read()

            code = enrich.main(["--date", "2026-09-15", "--input", in_path,
                                "--output", out_path])

            self.assertEqual(code, 0, "キー無しでも終了コード0で完走すること（C9）")
            self.assertEqual(open(in_path, "rb").read(), before,
                             "入力ファイルを書き換えてはいけない")
            with open(out_path, encoding="utf-8") as f:
                out = json.load(f)
            self.assertTrue(out["degraded"])
            self.assertEqual(out["engine"], "passthrough")

    def test_main_returns_one_when_input_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            code = enrich.main(["--date", "2026-09-15",
                               "--input", os.path.join(tmp, "nope.json"),
                               "--output", os.path.join(tmp, "out.json")])
        self.assertEqual(code, 1, "入力が無いのは処理不成立なので1")

    def test_dry_run_never_calls_api(self):
        day = load_fixture("day-multilang.json")
        self.install([])  # 呼ばれたら AssertionError
        os.environ["GEMINI_API_KEY"] = "would-be-real-key"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                in_path = os.path.join(tmp, "in.json")
                out_path = os.path.join(tmp, "out.json")
                with open(in_path, "w", encoding="utf-8") as f:
                    json.dump(day, f, ensure_ascii=False)
                code = enrich.main(["--input", in_path, "--output", out_path,
                                    "--dry-run"])
                self.assertEqual(code, 0)
                with open(out_path, encoding="utf-8") as f:
                    self.assertTrue(json.load(f)["degraded"])
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

    def test_api_key_never_appears_in_output_file(self):
        """キーがJSONに漏れないこと。"""
        day = load_fixture("day-multilang.json")
        ids = [a["article_id"] for a in day["articles"]]
        self.install([gemini_envelope([good_result(i) for i in ids])])
        secret = "AIzaSy-this-must-never-be-written"
        os.environ["GEMINI_API_KEY"] = secret
        try:
            with tempfile.TemporaryDirectory() as tmp:
                in_path = os.path.join(tmp, "in.json")
                out_path = os.path.join(tmp, "out.json")
                with open(in_path, "w", encoding="utf-8") as f:
                    json.dump(day, f, ensure_ascii=False)
                enrich.main(["--input", in_path, "--output", out_path])
                self.assertNotIn(secret, open(out_path, encoding="utf-8").read())
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

    def test_no_third_party_imports(self):
        """C8: 標準ライブラリのみ。pip install を要求しないこと。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "enrich.py")
        source = open(path, encoding="utf-8").read()
        for banned in ("google.generativeai", "google_generativeai",
                       "import requests", "import httpx", "import feedparser",
                       "from google import"):
            self.assertNotIn(banned, source, "%s は使えない（C8）" % banned)


if __name__ == "__main__":
    unittest.main(verbosity=2)
