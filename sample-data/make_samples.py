#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sample-data 生成スクリプト（④表示の目視確認専用）
============================================================
契約§4 の形に従って、④表示側だけで完結するサンプルを作る。
用途は「実データでは再現できない異常系の見た目確認」。
本番データ（data/）は一切触らない。scripts/ も触らない。

4パターン:
  1. 2026-09-20 正常日       status=ok      degraded=false  訳あり
  2. 2026-09-21 degraded日   status=ok      degraded=true   訳なし（原語のまま）
  3. 2026-09-22 取得失敗日   status=failed  topicsファイルなし
  4. 2026-09-23 データ無し日 status=empty   topics=[]（記事0件）
"""
import json, os, pathlib

BASE = pathlib.Path(__file__).resolve().parent
TOP = BASE / "topics"
MON = BASE / "months"
TOP.mkdir(parents=True, exist_ok=True)
MON.mkdir(parents=True, exist_ok=True)

CN = {"JP": "日本", "US": "米国", "GB": "英国", "FR": "フランス", "DE": "ドイツ",
      "RU": "ロシア", "CN": "中国", "KR": "韓国", "IN": "インド", "BR": "ブラジル",
      "SA": "サウジアラビア", "IL": "イスラエル"}
ALL_C = list(CN.keys())


def art(country, source, mt, lang, orig, ja, url, when, dir_="ltr"):
    """契約§4 articles[] ── 見出し＋リンクのみ。本文フィールドは作らない。"""
    import hashlib
    return {"article_id": hashlib.sha256(url.encode()).hexdigest()[:8],
            "country": country, "source": source, "media_type": mt, "lang": lang,
            "dir": dir_, "title_original": orig, "title_ja": ja,
            "url": url, "published_at": when}


def phr(country, media, mt, lang, orig, ja, stance, reason, url, dir_="ltr"):
    """契約§4 phrases[] ── 見出しの並置用。"""
    return {"country": country, "media": media, "media_type": mt, "lang": lang,
            "dir": dir_, "original": orig, "ja": ja,
            "stance": stance, "stance_reason": reason, "url": url}


def topic(tid, title, countries, arts, phrases, jp_mc, facts, tags, stances):
    """score は契約§4 A4「報道国数×3＋媒体数」で必ず計算する。手打ちしない。"""
    media = len({a["source"] for a in arts})
    score = len(countries) * 3 + media
    return {
        "topic_id": tid,
        "title_ja": title,
        "countries": countries,
        "media_count": media,
        "score": score,
        "score_parts": {"countries": len(countries), "countries_weight": 3, "media": media},
        "jp_reported": jp_mc > 0,
        "jp_media_count": jp_mc,
        "silent_countries": [c for c in ALL_C if c not in countries],
        "stance_counts": stances,
        "tags": tags,
        "facts_ja": facts,
        "phrases": phrases,
        "articles": arts,
    }


# ============================================================
# 1) 正常日 2026-09-20 ── 訳あり・タグあり・事実あり
# ============================================================
t1_arts = [
    art("US", "Reuters", "private", "en",
        "Central banks signal coordinated pause on rate hikes",
        "中央銀行、利上げの協調的停止を示唆",
        "https://example.com/reuters/cb-pause", "2026-09-20T01:10:00Z"),
    art("GB", "BBC News", "public", "en",
        "Rate pause agreed as inflation cools across major economies",
        "主要国のインフレ鈍化で利上げ停止に合意",
        "https://example.com/bbc/rate-pause", "2026-09-20T02:00:00Z"),
    art("FR", "Le Monde", "private", "fr",
        "Les banques centrales suspendent la hausse des taux",
        "中央銀行が利上げを停止",
        "https://example.com/lemonde/taux", "2026-09-20T03:25:00Z"),
    art("DE", "Deutsche Welle", "public", "de",
        "Notenbanken pausieren die Zinserhöhungen",
        "中央銀行が金利引き上げを一時停止",
        "https://example.com/dw/zinsen", "2026-09-20T04:05:00Z"),
    art("JP", "NHK", "public", "ja",
        "主要中銀 利上げ停止で協調姿勢", "主要中銀 利上げ停止で協調姿勢",
        "https://example.com/nhk/kinri", "2026-09-20T05:30:00Z"),
    art("CN", "Xinhua", "state", "zh",
        "主要央行暂停加息 释放协调信号", "主要中銀が利上げを停止し協調シグナルを発信",
        "https://example.com/xinhua/jiaxi", "2026-09-20T06:00:00Z"),
    art("SA", "Al Arabiya", "private", "ar",
        "البنوك المركزية توقف رفع أسعار الفائدة",
        "中央銀行、金利引き上げを停止",
        "https://example.com/alarabiya/faida", "2026-09-20T06:40:00Z", "rtl"),
]
t1_phr = [
    phr("US", "Reuters", "private", "en",
        "Central banks signal coordinated pause on rate hikes",
        "中央銀行、利上げの協調的停止を示唆", "neutral",
        "「示唆（signal）」と留保を付けた表現", "https://example.com/reuters/cb-pause"),
    phr("GB", "BBC News", "public", "en",
        "Rate pause agreed as inflation cools across major economies",
        "主要国のインフレ鈍化で利上げ停止に合意", "support",
        "「cools（鈍化）」を前面に置いた構成", "https://example.com/bbc/rate-pause"),
    phr("FR", "Le Monde", "private", "fr",
        "Les banques centrales suspendent la hausse des taux",
        "中央銀行が利上げを停止", "neutral",
        "事実の記述のみ", "https://example.com/lemonde/taux"),
    phr("DE", "Deutsche Welle", "public", "de",
        "Notenbanken pausieren die Zinserhöhungen",
        "中央銀行が金利引き上げを一時停止", "neutral",
        "「pausieren（一時停止）」と限定", "https://example.com/dw/zinsen"),
    phr("JP", "NHK", "public", "ja",
        "主要中銀 利上げ停止で協調姿勢", "主要中銀 利上げ停止で協調姿勢", "neutral",
        "体言止めで評価語を含まない", "https://example.com/nhk/kinri"),
    phr("CN", "Xinhua", "state", "zh",
        "主要央行暂停加息 释放协调信号", "主要中銀が利上げを停止し協調シグナルを発信",
        "support", "「释放（発信する）」と能動的に評価", "https://example.com/xinhua/jiaxi"),
    phr("SA", "Al Arabiya", "private", "ar",
        "البنوك المركزية توقف رفع أسعار الفائدة", "中央銀行、金利引き上げを停止",
        "neutral", "事実の記述のみ", "https://example.com/alarabiya/faida", "rtl"),
]
T1 = topic("T001", "主要中央銀行が利上げの一時停止を表明",
           ["US", "GB", "FR", "DE", "JP", "CN", "SA"], t1_arts, t1_phr, 1,
           ["9月19日、主要7か国の中央銀行が金融政策会合後の共同声明を公表した。",
            "対象となる政策金利の据え置き期間は少なくとも次回会合までとされた。"],
           ["経済", "金融政策"],
           {"support": 2, "critical": 0, "neutral": 5, "none": 0})

t2_arts = [
    art("RU", "TASS", "state", "ru",
        "Запущен новый газопровод в восточном направлении",
        "東方向けの新パイプラインが稼働開始",
        "https://example.com/tass/gaz", "2026-09-20T07:00:00Z"),
    art("CN", "Global Times", "state", "zh",
        "东线天然气管道投入运营 合作深化", "東線ガスパイプラインが運用開始、協力が深化",
        "https://example.com/gt/pipeline", "2026-09-20T07:30:00Z"),
    art("IN", "The Hindu", "private", "en",
        "New eastern gas pipeline begins operations",
        "東部の新ガスパイプラインが稼働開始",
        "https://example.com/thehindu/pipeline", "2026-09-20T08:10:00Z"),
    art("DE", "Der Spiegel", "private", "de",
        "Neue Gaspipeline nach Osten in Betrieb genommen",
        "東方への新ガスパイプラインが運転開始",
        "https://example.com/spiegel/pipeline", "2026-09-20T08:45:00Z"),
]
t2_phr = [
    phr("RU", "TASS", "state", "ru",
        "Запущен новый газопровод в восточном направлении",
        "東方向けの新パイプラインが稼働開始", "support",
        "「Запущен（launch された）」と成果として提示", "https://example.com/tass/gaz"),
    phr("CN", "Global Times", "state", "zh",
        "东线天然气管道投入运营 合作深化", "東線ガスパイプラインが運用開始、協力が深化",
        "support", "「合作深化（協力の深化）」と評価語を付加", "https://example.com/gt/pipeline"),
    phr("IN", "The Hindu", "private", "en",
        "New eastern gas pipeline begins operations",
        "東部の新ガスパイプラインが稼働開始", "neutral",
        "事実の記述のみ", "https://example.com/thehindu/pipeline"),
    phr("DE", "Der Spiegel", "private", "de",
        "Neue Gaspipeline nach Osten in Betrieb genommen",
        "東方への新ガスパイプラインが運転開始", "neutral",
        "受動態で評価を避けた表現", "https://example.com/spiegel/pipeline"),
]
# jp_media_count=0 → 日本での報道なし（jp_none グリッドに出る）
T2 = topic("T002", "東方向け天然ガスパイプラインが稼働開始",
           ["RU", "CN", "IN", "DE"], t2_arts, t2_phr, 0,
           ["9月19日、全長約2,600kmの新パイプラインで商業輸送が開始された。"],
           ["エネルギー"],
           {"support": 2, "critical": 0, "neutral": 2, "none": 0})

t3_arts = [
    art("IL", "The Times of Israel", "private", "he",
        "הושג הסכם חדש על סיוע הומניטרי", "人道支援に関する新たな合意が成立",
        "https://example.com/toi/aid", "2026-09-20T09:00:00Z", "rtl"),
    art("SA", "Arab News", "private", "ar",
        "اتفاق جديد على إيصال المساعدات الإنسانية",
        "人道支援の搬入に関する新合意",
        "https://example.com/arabnews/aid", "2026-09-20T09:20:00Z", "rtl"),
    art("US", "AP News", "private", "en",
        "Sides reach new humanitarian aid agreement",
        "両者が新たな人道支援合意に到達",
        "https://example.com/ap/aid", "2026-09-20T09:50:00Z"),
    art("JP", "朝日新聞", "private", "ja",
        "人道支援の搬入で新たな合意", "人道支援の搬入で新たな合意",
        "https://example.com/asahi/aid", "2026-09-20T10:30:00Z"),
    art("JP", "読売新聞", "private", "ja",
        "支援物資の搬入拡大で合意", "支援物資の搬入拡大で合意",
        "https://example.com/yomiuri/aid", "2026-09-20T10:40:00Z"),
    art("FR", "France 24", "public", "fr",
        "Nouvel accord sur l'aide humanitaire", "人道支援に関する新協定",
        "https://example.com/france24/aide", "2026-09-20T11:00:00Z"),
]
t3_phr = [
    phr("IL", "The Times of Israel", "private", "he",
        "הושג הסכם חדש על סיוע הומניטרי", "人道支援に関する新たな合意が成立",
        "neutral", "受動態で主体を示さない構成", "https://example.com/toi/aid", "rtl"),
    phr("SA", "Arab News", "private", "ar",
        "اتفاق جديد على إيصال المساعدات الإنسانية", "人道支援の搬入に関する新合意",
        "neutral", "事実の記述のみ", "https://example.com/arabnews/aid", "rtl"),
    phr("US", "AP News", "private", "en",
        "Sides reach new humanitarian aid agreement", "両者が新たな人道支援合意に到達",
        "neutral", "「Sides（両者）」と主体を並列化", "https://example.com/ap/aid"),
    phr("JP", "朝日新聞", "private", "ja",
        "人道支援の搬入で新たな合意", "人道支援の搬入で新たな合意", "neutral",
        "体言止めで評価語を含まない", "https://example.com/asahi/aid"),
    phr("JP", "読売新聞", "private", "ja",
        "支援物資の搬入拡大で合意", "支援物資の搬入拡大で合意", "support",
        "「拡大」を加えて進展を強調", "https://example.com/yomiuri/aid"),
    phr("FR", "France 24", "public", "fr",
        "Nouvel accord sur l'aide humanitaire", "人道支援に関する新協定", "neutral",
        "事実の記述のみ", "https://example.com/france24/aide"),
]
T3 = topic("T003", "人道支援の搬入に関する新たな合意",
           ["IL", "SA", "US", "JP", "FR"], t3_arts, t3_phr, 2,
           ["9月19日、支援物資の搬入ルート拡大について合意文書が交わされた。"],
           ["国際", "人道"],
           {"support": 1, "critical": 0, "neutral": 5, "none": 0})

t4_arts = [
    art("KR", "Yonhap News", "private", "ko",
        "반도체 수출 규제 완화 합의", "半導体輸出規制の緩和で合意",
        "https://example.com/yonhap/semi", "2026-09-20T12:00:00Z"),
    art("US", "Bloomberg", "private", "en",
        "Chip export curbs eased in narrow deal",
        "限定的な取引で半導体輸出規制が緩和",
        "https://example.com/bloomberg/chips", "2026-09-20T12:20:00Z"),
    art("JP", "日経新聞", "private", "ja",
        "半導体規制、一部緩和で合意", "半導体規制、一部緩和で合意",
        "https://example.com/nikkei/semi", "2026-09-20T12:50:00Z"),
]
t4_phr = [
    phr("KR", "Yonhap News", "private", "ko",
        "반도체 수출 규제 완화 합의", "半導体輸出規制の緩和で合意", "support",
        "「완화(緩和)」を前面に出した構成", "https://example.com/yonhap/semi"),
    phr("US", "Bloomberg", "private", "en",
        "Chip export curbs eased in narrow deal", "限定的な取引で半導体輸出規制が緩和",
        "neutral", "「narrow（限定的）」と範囲を限定", "https://example.com/bloomberg/chips"),
    phr("JP", "日経新聞", "private", "ja",
        "半導体規制、一部緩和で合意", "半導体規制、一部緩和で合意", "neutral",
        "「一部」と限定を付した表現", "https://example.com/nikkei/semi"),
]
T4 = topic("T004", "半導体輸出規制の一部緩和で合意",
           ["KR", "US", "JP"], t4_arts, t4_phr, 1,
           ["9月19日、特定用途向け半導体の輸出手続きを簡素化する措置が発表された。"],
           ["経済", "技術"],
           {"support": 1, "critical": 0, "neutral": 2, "none": 0})

t5_arts = [
    art("BR", "Folha de S.Paulo", "private", "pt",
        "Recorde de desmatamento evitado na Amazônia",
        "アマゾンで森林減少の抑制が過去最高",
        "https://example.com/folha/amazonia", "2026-09-20T13:00:00Z"),
    art("GB", "The Guardian", "private", "en",
        "Amazon deforestation falls to record low",
        "アマゾンの森林破壊が過去最低に",
        "https://example.com/guardian/amazon", "2026-09-20T13:30:00Z"),
]
t5_phr = [
    phr("BR", "Folha de S.Paulo", "private", "pt",
        "Recorde de desmatamento evitado na Amazônia",
        "アマゾンで森林減少の抑制が過去最高", "support",
        "「Recorde（記録）」を成果として提示", "https://example.com/folha/amazonia"),
    phr("GB", "The Guardian", "private", "en",
        "Amazon deforestation falls to record low", "アマゾンの森林破壊が過去最低に",
        "support", "「record low」と改善を強調", "https://example.com/guardian/amazon"),
]
T5 = topic("T005", "アマゾンの森林減少が過去最低水準に",
           ["BR", "GB"], t5_arts, t5_phr, 0,
           ["9月18日、衛星観測にもとづく年次集計値が公表された。"],
           ["環境"],
           {"support": 2, "critical": 0, "neutral": 0, "none": 0})

normal = {
    "schema_version": 3,
    "date": "2026-09-20",
    "generated_at": "2026-09-20T15:00:00Z",
    "degraded": False,
    "score_formula": "報道した国の数 × 3 ＋ 掲載媒体の数 × 1",
    "stats": {"articles": 23, "topics": 5, "countries_covered": 7,
              "feeds_ok": 27, "feeds_stale": 0, "feeds_empty": 0, "feeds_failed": 0,
              "jp_none_count": 2},
    "alerts": [],
    "topics": sorted([T1, T2, T3, T4, T5], key=lambda t: (-t["score"], t["topic_id"])),
}

# ============================================================
# 2) degraded 日 2026-09-21 ── GEMINI_API_KEY 未設定を再現
#    title_ja は原語のまま／facts_ja・tags は空／stance は none
# ============================================================
def degrade(t):
    d = json.loads(json.dumps(t, ensure_ascii=False))
    for a in d["articles"]:
        a["title_ja"] = a["title_original"]
    for p in d["phrases"]:
        p["ja"] = p["original"]
        p["stance"] = "none"
        p["stance_reason"] = ""
    d["stance_counts"] = {"support": 0, "critical": 0, "neutral": 0,
                          "none": len(d["phrases"])}
    d["tags"] = []
    d["facts_ja"] = []
    # 原語見出しをそのまま話題名にする（AI要約が無いため）
    d["title_ja"] = d["articles"][0]["title_original"]
    return d

dg = [degrade(t) for t in (T1, T2, T3, T4, T5)]
for i, t in enumerate(dg, 1):
    t["topic_id"] = "T%03d" % i

degraded_day = {
    "schema_version": 3,
    "date": "2026-09-21",
    "generated_at": "2026-09-21T15:00:00Z",
    "degraded": True,
    "score_formula": "報道した国の数 × 3 ＋ 掲載媒体の数 × 1",
    "stats": {"articles": 23, "topics": 5, "countries_covered": 7,
              "feeds_ok": 27, "feeds_stale": 0, "feeds_empty": 0, "feeds_failed": 0,
              "jp_none_count": 2},
    "alerts": [{"level": "warn", "code": "ai_unavailable",
                "message": "GEMINI_API_KEY が未設定のため、翻訳・要約・立場ラベルを付与できませんでした。見出しは原語のまま表示しています。"}],
    "topics": sorted(dg, key=lambda t: (-t["score"], t["topic_id"])),
}

# 4) データ無し日 2026-09-23 ── topics は空配列（ファイルは存在する）
nodata_day = {
    "schema_version": 3,
    "date": "2026-09-23",
    "generated_at": "2026-09-23T15:00:00Z",
    "degraded": False,
    "score_formula": "報道した国の数 × 3 ＋ 掲載媒体の数 × 1",
    "stats": {"articles": 0, "topics": 0, "countries_covered": 0,
              "feeds_ok": 27, "feeds_stale": 0, "feeds_empty": 0, "feeds_failed": 0,
              "jp_none_count": 0},
    "alerts": [{"level": "info", "code": "no_topics",
                "message": "RSS取得は成功しましたが、2か国以上で共通して報じられた話題が見つかりませんでした。"}],
    "topics": [],
}

json.dump(normal, open(TOP / "2026-09-20.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
json.dump(degraded_day, open(TOP / "2026-09-21.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
# 3) 取得失敗日 2026-09-22 は topics ファイルを作らない（404 になる想定）
json.dump(nodata_day, open(TOP / "2026-09-23.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

# ============================================================
# 月シャード + index
# ============================================================
month = {
    "schema_version": 3,
    "month": "2026-09",
    "days": {
        # 1) 正常日
        "2026-09-20": {"status": "ok", "articles": 23, "countries": 7,
                       "feeds_ok": 27, "feeds_total": 27,
                       "generated_at": "2026-09-20T15:00:00Z", "alerts": []},
        # 2) degraded 日（取得は成功。AI未適用は topics 側の degraded:true で示す）
        "2026-09-21": {"status": "ok", "articles": 23, "countries": 7,
                       "feeds_ok": 27, "feeds_total": 27,
                       "generated_at": "2026-09-21T15:00:00Z",
                       "alerts": [{"feed_id": "-", "source": "AI付与",
                                   "status": "failed",
                                   "note": "GEMINI_API_KEY が未設定のため翻訳・要約・立場ラベルを付与できませんでした。"}]},
        # 3) 取得失敗日：全フィード失敗 → topics ファイルは作られない（契約 status=failed）
        "2026-09-22": {"status": "failed", "articles": 0, "countries": 0,
                       "feeds_ok": 0, "feeds_total": 27,
                       "generated_at": "2026-09-22T15:00:00Z",
                       "alerts": [{"feed_id": "*", "source": "全フィード",
                                   "status": "failed",
                                   "note": "27フィードすべてが取得に失敗しました（ネットワーク到達不能）。"}]},
        # 4) データ無し日：HTTP 200 だが記事0件（契約 status=empty）→ topics=[]
        "2026-09-23": {"status": "empty", "articles": 0, "countries": 0,
                       "feeds_ok": 0, "feeds_total": 27,
                       "generated_at": "2026-09-23T15:00:00Z",
                       "alerts": [{"feed_id": "*", "source": "全フィード",
                                   "status": "empty",
                                   "note": "取得は成功しましたが記事が0件でした。"}]},
    },
    "updated_at": "2026-09-23T15:05:00Z",
}
index = {
    "schema_version": 3,
    "months": ["2026-09"],
    "latest_date": "2026-09-20",
    "updated_at": "2026-09-23T15:05:00Z",
}
json.dump(month, open(MON / "2026-09.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
json.dump(index, open(BASE / "index.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

# ============================================================
# 自己検証：契約 A4（score = 国数×3＋媒体数）と A5（score 降順）
# ============================================================
ok = True
for name, day in (("normal", normal), ("degraded", degraded_day), ("nodata", nodata_day)):
    ts = day["topics"]
    for t in ts:
        exp = len(t["countries"]) * 3 + t["media_count"]
        if t["score"] != exp:
            print("A4 NG", name, t["topic_id"], t["score"], exp); ok = False
        if t["jp_reported"] != (t["jp_media_count"] > 0):
            print("jp_reported NG", name, t["topic_id"]); ok = False
    for i in range(len(ts) - 1):
        if ts[i]["score"] < ts[i + 1]["score"]:
            print("A5 NG", name, i); ok = False
    print(f"{name:9s} topics={len(ts)} scores={[t['score'] for t in ts]} "
          f"jp_none={[t['topic_id'] for t in ts if not t['jp_reported']]}")
print("A4/A5 self-check:", "PASS" if ok else "FAIL")
print("wrote:", sorted(p.name for p in TOP.iterdir()), "| months:", sorted(p.name for p in MON.iterdir()))
