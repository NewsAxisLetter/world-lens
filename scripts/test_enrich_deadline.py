#!/usr/bin/env python3
"""35分タイムアウト（2026-09-27 の実ログ）の再発防止テスト。

仮想時計を使うので実時間では待たない。
- 2,793件・遅いAPI（1リクエスト120秒）でも、締切（20分）を越えずに終わる
- 503 が続いても、待ち時間が締切を越えない
- AI対象の上限と、国・媒体の均等選択
- 途中保存（checkpoint）が呼ばれる
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich  # noqa: E402
from test_enrich import gemini_envelope, good_result, GoogleError  # noqa: E402


class VirtualClock(object):
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        assert s >= 0, "負の待ち時間"
        self.t += s


def make_day(n_articles, countries=33, feeds_per_country=3):
    arts = []
    i = 0
    while len(arts) < n_articles:
        c = "C%02d" % (i % countries)
        f = (i // countries) % feeds_per_country
        arts.append({
            "article_id": "a%05d" % i,
            "country": c,
            "source": "%s-src%d" % (c, f),
            "media_type": "private",
            "lang": "en",
            "title": "Headline %d" % i,
            "url": "https://example.com/%d" % i,
            "published_at": "2026-09-27T00:00:00Z",
        })
        i += 1
    return {"date": "2026-09-27", "articles": arts, "status": "ok"}


class SlowGemini(object):
    """1リクエストごとに仮想時間を進める偽API。mode で成功/503を切替。"""

    def __init__(self, clock, latency=120.0, mode="ok"):
        self.clock = clock
        self.latency = latency
        self.mode = mode
        self.calls = 0
        self.timeouts = []

    def __call__(self, url, payload, api_key, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        # 実際の urlopen と同じく、timeout を越えて待つことはない
        spent = self.latency if timeout is None else min(self.latency, timeout)
        self.clock.t += spent
        if self.mode == "503":
            e = GoogleError(503, "UNAVAILABLE")
            raise enrich._api_error_from_http(e.status, e.body.encode("utf-8"), e.headers)
        if timeout is not None and self.latency > timeout:
            raise enrich.ApiError("timed out", status=None, retryable=True)
        text = payload["contents"][0]["parts"][0]["text"]
        ids = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{") and '"id"' in line:
                try:
                    ids.append(json.loads(line)["id"])
                except Exception:
                    pass
        if not ids:
            # 入力の形式が変わっていても id を拾えるよう、正規表現でも探す
            import re
            ids = re.findall(r'"id"\s*:\s*"([^"]+)"', text)
        return gemini_envelope([good_result(x) for x in ids])


class DeadlineTests(unittest.TestCase):
    DEADLINE = 20 * 60

    def run_enrich(self, n, latency, mode="ok", max_ai=600, checkpoint=None):
        clock = VirtualClock()
        api = SlowGemini(clock, latency=latency, mode=mode)
        payload = enrich.enrich(
            make_day(n), date_key="2026-09-27", api_key="dummy",
            poster=api, sleeper=clock.sleep, clock=clock,
            deadline_sec=self.DEADLINE, max_ai_articles=max_ai,
            checkpoint=checkpoint)
        return payload, clock, api

    def test_real_log_volume_slow_api_finishes_before_deadline(self):
        # 実ログと同じ 2,793件、1リクエスト80秒の遅いAPI、上限なし（旧コードの条件）
        payload, clock, api = self.run_enrich(2793, latency=80.0, max_ai=0)
        self.assertLessEqual(clock.t, self.DEADLINE + 5,
                             "締切 %ds を越えた: %.0fs" % (self.DEADLINE, clock.t))
        self.assertEqual(len(payload["articles"]), 2793, "記事が失われた")
        done = sum(1 for a in payload["articles"] if a.get("enriched_by") == enrich.BY_GEMINI)
        self.assertGreater(done, 0)

    def test_all_503_never_sleeps_past_deadline(self):
        payload, clock, api = self.run_enrich(2793, latency=5.0, mode="503", max_ai=0)
        self.assertLessEqual(clock.t, self.DEADLINE + 5, "%.0fs" % clock.t)
        self.assertEqual(len(payload["articles"]), 2793)
        self.assertTrue(payload["degraded"])

    def test_hanging_api_is_cut_by_request_timeout(self):
        # 応答が1時間返らないAPIでも締切で止まる
        payload, clock, api = self.run_enrich(600, latency=3600.0)
        self.assertLessEqual(clock.t, self.DEADLINE + 5, "%.0fs" % clock.t)
        self.assertTrue(all(t is not None and t <= enrich.TIMEOUT_SEC for t in api.timeouts))

    def test_fast_api_translates_the_cap(self):
        payload, clock, api = self.run_enrich(2793, latency=8.0, max_ai=600)
        done = [a for a in payload["articles"] if a.get("enriched_by") == enrich.BY_GEMINI]
        self.assertEqual(len(done), 600)
        self.assertEqual(len(payload["articles"]), 2793)
        # 33カ国すべてに翻訳が行き渡る
        country_of = {a["article_id"]: a["country"] for a in make_day(2793)["articles"]}
        self.assertEqual(len({country_of[a["article_id"]] for a in done}), 33)

    def test_checkpoint_called(self):
        saved = []
        self.run_enrich(600, latency=8.0, checkpoint=lambda p: saved.append(p))
        self.assertTrue(saved, "途中保存が一度も呼ばれない")
        for p in saved:
            enrich.assert_no_body_fields(p, "enriched")


class PerModelGemini(object):
    """モデルごとに振る舞いを変える当偽API（スレッド安全）。実時間は待たない。
    behavior[model] = "ok" | "503" | "quota"(日次上限) | "fatal"(キー無効)"""

    def __init__(self, behavior):
        import threading
        self.behavior = behavior
        self.lock = threading.Lock()
        self.calls = {}
        self.seen_ids = []

    def __call__(self, url, payload, api_key, timeout=None):
        import re
        model = re.search(r"models/([^:]+):", url).group(1)
        with self.lock:
            self.calls[model] = self.calls.get(model, 0) + 1
        b = self.behavior.get(model, "ok")
        if b == "503":
            e = GoogleError(503, "UNAVAILABLE")
            raise enrich._api_error_from_http(e.status, e.body.encode("utf-8"), e.headers)
        if b == "quota":
            # 本物の日次上限エラーと同じ形（quotaId に PerDay が入る）
            e = GoogleError(429, "RESOURCE_EXHAUSTED",
                            quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier")
            raise enrich._api_error_from_http(e.status, e.body.encode("utf-8"), e.headers)
        if b == "fatal":
            # 本物のキー無効エラーと同じ形（reason=API_KEY_INVALID）
            e = GoogleError(400, "INVALID_ARGUMENT", reason="API_KEY_INVALID")
            raise enrich._api_error_from_http(e.status, e.body.encode("utf-8"), e.headers)
        text = payload["contents"][0]["parts"][0]["text"]
        ids = [x for x in re.findall(r'"id"\s*:\s*"([^"<]+)"', text)]
        with self.lock:
            self.seen_ids.extend(ids)
        return gemini_envelope([good_result(x) for x in ids])


class ParallelTests(unittest.TestCase):
    MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite"]

    def run_par(self, n, behavior, parallel=3, checkpoint=None):
        api = PerModelGemini(behavior)
        payload = enrich.enrich(
            make_day(n), date_key="2026-09-27", api_key="dummy",
            poster=api, sleeper=lambda s: None, pace=0.0, models=self.MODELS,
            deadline_sec=20 * 60, max_ai_articles=0, checkpoint=checkpoint,
            parallel=parallel, defer_wait=0.0)
        done = [a for a in payload["articles"] if a.get("enriched_by") == enrich.BY_GEMINI]
        return payload, api, done

    def test_all_2734_articles_with_three_models(self):
        # 実ログ（2026-09-28）と同じ 2,734件。上限なし・3モデル同時で全件翻訳される
        payload, api, done = self.run_par(2734, {})
        self.assertEqual(len(payload["articles"]), 2734)
        self.assertEqual(len(done), 2734, "全件が翻訳されていない")
        # 3モデルすべてが使われている（並列に分散）
        for m in self.MODELS:
            self.assertGreater(api.calls.get(m, 0), 10, "%s がほとんど使われていない: %s" % (m, api.calls))
        # 同じ記事を二重に送っていない（無料枠の無駄遣いなし）
        self.assertEqual(len(api.seen_ids), len(set(api.seen_ids)))
        self.assertFalse(payload["degraded"])
        enrich.assert_no_body_fields(payload, "enriched")

    def test_one_model_daily_quota_others_take_over(self):
        # 1番目のモデルが日次上限 → 他の2モデルで全件処理
        payload, api, done = self.run_par(1000, {"gemini-3.5-flash-lite": "quota"})
        self.assertEqual(len(done), 1000)
        self.assertLessEqual(api.calls.get("gemini-3.5-flash-lite", 0), 3,
                             "日次上限のモデルに送り続けている")

    def test_one_model_overloaded_others_take_over(self):
        payload, api, done = self.run_par(1000, {"gemini-3.8-flash": "503"})
        self.assertEqual(len(done), 1000)

    def test_all_overloaded_keeps_articles_and_stops(self):
        b = {m: "503" for m in self.MODELS}
        payload, api, done = self.run_par(500, b)
        self.assertEqual(len(done), 0)
        self.assertEqual(len(payload["articles"]), 500)
        self.assertTrue(payload["degraded"])

    def test_invalid_key_stops_everything_fast(self):
        b = {m: "fatal" for m in self.MODELS}
        payload, api, done = self.run_par(500, b)
        self.assertLessEqual(sum(api.calls.values()), 3, "キー無効なのに送り続けた: %s" % api.calls)
        self.assertEqual(len(payload["articles"]), 500)

    def test_parallel_checkpoint_is_consistent(self):
        saved = []
        payload, api, done = self.run_par(1000, {}, checkpoint=lambda p: saved.append(p))
        self.assertTrue(saved)
        for p in saved:
            self.assertEqual(len(p["articles"]), 1000, "途中保存で記事が欠けた")
            enrich.assert_no_body_fields(p, "enriched")

    def test_parallel_1_is_sequential_fallback(self):
        payload, api, done = self.run_par(300, {}, parallel=1)
        self.assertEqual(len(done), 300)
        self.assertEqual(set(api.calls), {"gemini-3.5-flash-lite"})


class SelectionTests(unittest.TestCase):
    def test_under_limit_unchanged(self):
        todo = make_day(10)["articles"]
        self.assertEqual(enrich.select_for_ai(todo, 600), todo)

    def test_no_limit(self):
        todo = make_day(50)["articles"]
        self.assertEqual(enrich.select_for_ai(todo, 0), todo)

    def test_balanced_by_country_and_source(self):
        # 先頭の国に大量、後ろの国に少し、という偏ったデータ
        todo = []
        for i in range(500):
            todo.append({"article_id": "x%d" % i, "country": "AE", "source": "AE-%d" % (i % 2)})
        for i in range(20):
            todo.append({"article_id": "z%d" % i, "country": "ZA", "source": "ZA-0"})
        sel = enrich.select_for_ai(todo, 40)
        self.assertEqual(len(sel), 40)
        za = [a for a in sel if a["country"] == "ZA"]
        ae = [a for a in sel if a["country"] == "AE"]
        self.assertEqual(len(za), 20)
        self.assertEqual(len(ae), 20)
        # AE の中でも2媒体に均等
        self.assertEqual(len([a for a in ae if a["source"] == "AE-0"]), 10)
        # 元の並び順を保つ
        idx = [todo.index(a) for a in sel]
        self.assertEqual(idx, sorted(idx))


class EnvTests(unittest.TestCase):
    def test_env_int(self):
        os.environ["X_TEST_INT"] = ""
        self.assertEqual(enrich._env_int("X_TEST_INT", 7), 7)
        os.environ["X_TEST_INT"] = "abc"
        self.assertEqual(enrich._env_int("X_TEST_INT", 7), 7)
        os.environ["X_TEST_INT"] = "12"
        self.assertEqual(enrich._env_int("X_TEST_INT", 7), 12)
        del os.environ["X_TEST_INT"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
