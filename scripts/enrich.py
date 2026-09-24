#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ1 ②AI付与 ── data/days/D.json（契約§2）に日本語訳・要約・タグ・論調を足して
data/days/D.enriched.json（契約§3）を書き出す。

設計方針（`phase1/データ契約.md` §0 / §3 と `最終検証レポート.md` §1）
------------------------------------------------------------------------------
* **入力ファイルは絶対に書き換えない**（契約§3 冒頭）。読むだけ。出力は別ファイル。
  → AIの結果が気に入らなければ .enriched.json を消して再実行するだけで戻せる。

* **キーが無くても落ちない**（C9・A3・A9）。ここが全体の生命線。
  GEMINI_API_KEY が「未設定」「不正」「全リクエスト失敗」のどれであっても
  例外を投げずに終了コード0で完走し、`degraded: true` ＋ 各記事
  `enriched_by: "passthrough"` を出す。summary_ja は **空文字**にする。
  AIが動かなかったときに要約を作文したら、それは事実でない文章を公開することになる。
  「空にする」以外の選択肢は取らない（契約§3 フォールバック表）。

* **標準ライブラリのみ**（C8）。google-generativeai は pip が必要なので使えない。
  REST（generativeLanguage v1beta :generateContent）を urllib で直に叩く。

* **1リクエストに複数記事**（契約§3・A12）。1記事1リクエストは禁止。
  既定10件/リクエスト。240記事なら24リクエストで、目標30以内・上限250/日に収まる。
  実測上限の根拠：`最終検証レポート.md` §1-1（2.5 Flash = RPD 250 / RPM 10 / TPM 250,000）。

* **部分失敗を全体失敗にしない**（契約§3）。
  バッチが3回リトライしても駄目ならそのバッチだけ passthrough にして次のバッチへ進む。
  JSONのパースに失敗した場合も、壊れていた記事だけを passthrough にする。

* **本文を使わない・持たない**（C1・C2）。AIに渡すのは見出しだけ。
  出力キーも契約§3のホワイトリストに固定し、最後に本文系キーの再帰チェックをかける。

* **APIキーは環境変数 GEMINI_API_KEY からのみ読む**。
  URLクエリではなく `x-goog-api-key` ヘッダで送る（ログやエラー文にURLが出てもキーが漏れない）。
  ソース・JSON・標準出力のどこにもキーを書かない。

使い方
------
  python3 scripts/enrich.py                        # JSTの今日を処理
  python3 scripts/enrich.py --date 2026-09-15      # 日付を指定
  python3 scripts/enrich.py --input x.json --output y.json   # ファイル直指定（テスト用）
  python3 scripts/enrich.py --batch-size 10 --max-requests 30

終了コード
----------
  0 = 完走（degraded: true でも0。サイトは描けるので「赤」にしない）
  1 = 入力ファイルが読めない／出力が書けない（＝そもそも処理が成立していない）
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# ==================================================================
# 設定
# ==================================================================

SCHEMA_VERSION = 3          # 契約§3
MODEL = "gemini-2.5-flash"  # 契約§3 で指定。他モデルに変えると無料枠の実測値が変わる
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 1リクエストにまとめる記事数。
#   240記事 ÷ 10件 = 24リクエスト（最終検証レポート §1-1 の試算と同じ）。
#   大きくするとリクエスト数は減るが、1件の壊れたレスポンスで巻き込む記事が増える。
#   10件はレポートの試算値（1リクエスト約7,500トークン＝TPM 250,000 の33分の1）。
BATCH_SIZE = 10

# 1日に出してよいHTTPリクエストの上限（リトライも含めて数える）。
#   契約§3「目標：1日あたり30リクエスト以内（上限250の1/8）」＝A12 の検算対象。
#   リトライ込みでここを超えないよう、残りバッチは passthrough に落とす。
REQUEST_BUDGET = 30

# 通常バッチ（リトライを除く）の本数の上限。
#   REQUEST_BUDGET 30 のうち 20 を本番、10 をリトライ用の余裕として残す。
#   記事が200件を超えても batch_size を自動で広げてこの本数に収める。
MAX_BATCHES = 20

MAX_RETRY = 3               # 契約§3「429/5xxは指数バックオフで最大3回」
BACKOFF_BASE_SEC = 2.0      # 2秒 → 4秒 → 8秒（指数バックオフ）
TIMEOUT_SEC = 120           # 生成は時間がかかるのでRSS取得(20秒)より長く取る

# RPM 10（最終検証レポート §1-1）に対して6秒間隔なら毎分10リクエストで収まる。
# 24リクエストでも約2.4分。1日1回のバッチ処理なので待ち時間は問題にならない。
PACE_SEC = 6.0

# 契約§3 の固定タグ集合。ここに無い語が返ってきたら捨てる（増やすときは契約が先）。
ALLOWED_TAGS = (
    "政治", "経済", "安全保障", "外交", "気候", "人権",
    "科学技術", "保健", "社会", "文化", "スポーツ", "災害",
)
MAX_TAGS = 3                # 契約§3「1〜3個」

ALLOWED_STANCES = ("support", "critical", "neutral")

# 出力する記事1件のキー。契約§3 のとおり。ここに無いキーは出さない（C1の担保）。
ARTICLE_FIELDS = (
    "article_id",
    "title_ja",
    "summary_ja",
    "tags",
    "stance",
    "stance_reason",
    "key_phrase_original",
    "key_phrase_ja",
    "enriched_by",
)

# 本文が入りうるキー。1つでも出力に現れたら設計違反なので実行時に止める（C1・A1）。
FORBIDDEN_FIELDS = (
    "body", "content", "content_encoded", "encoded",
    "description", "summary", "text", "fulltext", "abstract",
)

ENGINE_PASSTHROUGH = "passthrough"
BY_GEMINI = "gemini"
BY_PASSTHROUGH = "passthrough"
REASON_NO_AI = "AI未適用"       # 契約§3 フォールバック表の文言をそのまま使う

# summary_ja の長さの上限（文字）。2文に切ったうえで、なお長い場合の保険。
SUMMARY_MAX_CHARS = 240

# 推測・評価を含む文をはじくための語。C2「事実のみ」をプロンプトだけに頼らず
# 出力側でも機械的に確認する。引っかかったら要約を**空にする**（作文しない）。
SPECULATION_MARKERS = (
    "だろう", "かもしれない", "と思われる", "と見られる", "に違いない",
    "べきだ", "ではないか", "可能性が高い", "予想される", "期待される",
)

JST = timezone(timedelta(hours=9), "JST")

# このファイルの1つ上（= phase1/）を基準にする。どこから実行しても data/ がズレない。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
DAYS_DIR = os.path.join(DATA_DIR, "days")


# ==================================================================
# 小さな道具
# ==================================================================

def utc_now_iso():
    """現在時刻をUTCのISO8601（秒まで・末尾Z）で返す。フェーズ0と同じ形式。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def jst_today():
    """処理対象日の既定値（JST基準）。日別ファイルの日付キーはJSTで決める。"""
    return datetime.now(JST).strftime("%Y-%m-%d")


def day_path_for(date_key):
    return os.path.join(DAYS_DIR, "%s.json" % date_key)


def enriched_path_for(date_key):
    return os.path.join(DAYS_DIR, "%s.enriched.json" % date_key)


def load_json(path):
    """JSONを読む。読めなければ None（呼び出し側で「入力なし」として扱う）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print("  入力を読めません: %s (%s: %s)" % (path, type(e).__name__, e))
        return None


def keep_generated_at_if_unchanged(path, payload, stamp_key="generated_at"):
    """中身が前回と同じなら generated_at を据え置く（A2：真の冪等）。

    これが無いと、記事が1件も変わらない日でも generated_at だけが毎回変わり、
    git が 1MB 級の blob を毎日作り直す。1年で履歴が GB 規模に膨らむため、
    「内容が同じなら1バイトも変えない」ことを書き込み層で保証する。
    """
    if not os.path.exists(path):
        return payload
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return payload
    if not isinstance(old, dict) or stamp_key not in old:
        return payload
    probe = dict(payload)
    probe[stamp_key] = old[stamp_key]
    if probe == old:
        return probe
    return payload


def save_json(path, payload):
    """JSONを書く。UTF-8・日本語そのまま・末尾改行（gitの差分が読みやすい）。"""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = keep_generated_at_if_unchanged(path, payload)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")


def assert_no_body_fields(payload, where="enriched"):
    """本文系キーが出力に1つも無いことを確認する（C1・A1の自己チェック）。

    ホワイトリストで組み立てているので理屈の上では入り得ないが、
    将来 ARTICLE_FIELDS に "summary" などを足してしまう事故を実行時に止める最後の砦。
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in FORBIDDEN_FIELDS:
                    found.append("%s.%s" % (path, key))
                walk(value, "%s.%s" % (path, key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, "%s[%d]" % (path, i))

    walk(payload, where)
    if found:
        raise RuntimeError(
            "本文系のキーが出力に含まれています（設計違反）: %s" % ", ".join(found))
    return True


def _sleep(seconds):
    """待機。テストから差し替えて即時実行できるよう関数に切り出してある。"""
    if seconds > 0:
        time.sleep(seconds)


# GitHub Secrets 未設定のまま .env をコピーした時に紛れ込む定番の埋め草。
# これを本物として扱うと 400 を4回投げてから素通しに落ちる（無駄なリクエストと待ち時間）。
# 未設定と同じ扱いにして0リクエストで素通しさせる。
PLACEHOLDER_KEYS = frozenset([
    "your-api-key", "your_api_key", "yourapikey",
    "changeme", "change-me", "dummy", "none", "null",
    "xxx", "todo", "secret", "apikey", "api-key", "api_key",
    "gemini_api_key", "replace-me", "placeholder", "undefined",
])


def read_api_key():
    """APIキーは環境変数からのみ読む。空白だけ・埋め草の値は「未設定」と同じ扱い。

    返り値をログに出してはいけない。呼び出し側は bool() だけを表示する。
    """
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key.lower() in PLACEHOLDER_KEYS:
        return ""
    return key


# ==================================================================
# 入力の読み取り（契約§2）
# ==================================================================

def valid_articles(day_payload):
    """①の出力から、②が処理できる記事だけを取り出す。

    C4：url が無い記事は捨てる（根拠を追跡できないものは載せない）。
    article_id が欠けている入力（手書きの実験データなど）は url から作り直す。
    契約§2 の定義 sha256(url).hexdigest()[:8] と同じ式を使うので①と必ず一致する。
    """
    if not isinstance(day_payload, dict):
        return []
    raw = day_payload.get("articles")
    if not isinstance(raw, list):
        return []

    out = []
    seen_ids = set()
    seen_urls = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue                       # C4：原文リンクの無い記事は捨てる
        url = url.strip()
        article_id = item.get("article_id")
        if not isinstance(article_id, str) or not article_id:
            article_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
        # URLとIDの両方で重複を見る（A2 冪等）。
        # 契約§2 では article_id = sha256(url)[:8] なので本来は同じURLなら同じIDになるが、
        # 手書きデータや①以外が作ったJSONではIDが式と一致しないことがある。
        # IDだけで見ると同じ記事が2回採用され、比較表で二重計上になる。
        if article_id in seen_ids or url in seen_urls:
            continue
        seen_ids.add(article_id)
        seen_urls.add(url)
        title = item.get("title_original")
        out.append({
            "article_id": article_id,
            "title_original": title if isinstance(title, str) else "",
            "lang": item.get("lang") if isinstance(item.get("lang"), str) else "",
            "source": item.get("source") if isinstance(item.get("source"), str) else "",
            "country": item.get("country") if isinstance(item.get("country"), str) else "",
        })
    return out


# ==================================================================
# 素通し（passthrough）── ここが C9 / A3 / A9 の本体
# ==================================================================

def passthrough_article(article):
    """AIを適用できなかった記事1件の出力を作る（契約§3 フォールバック表そのまま）。

    * title_ja = title_original（未翻訳であることが画面で分かる）
    * summary_ja = ""  ← **絶対に捏造しない**。AIが答えていない要約は存在しない。
    * tags = []、stance = "neutral"、stance_reason = "AI未適用"
    * key_phrase_original = 原語の見出しをそのまま（C5：原語を潰さない）
    * key_phrase_ja = ""（訳は無い）
    """
    title = article.get("title_original") or ""
    return {
        "article_id": article["article_id"],
        "title_ja": title,
        "summary_ja": "",
        "tags": [],
        "stance": "neutral",
        "stance_reason": REASON_NO_AI,
        "key_phrase_original": title,
        "key_phrase_ja": "",
        "enriched_by": BY_PASSTHROUGH,
    }


def build_payload(date_key, articles, api_calls, degraded, engine, notes=None):
    """契約§3 の出力JSONを組み立てる。キーの順序も契約の記載順に合わせる。"""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "date": date_key,
        "generated_at": utc_now_iso(),
        "engine": engine,
        "api_calls": api_calls,
        "degraded": bool(degraded),
        "articles": articles,
    }
    if notes:
        # 欠損・失敗を隠さない（C7）。何が起きて degraded になったかを画面と人間に残す。
        payload["notes"] = list(notes)
    return payload


# ==================================================================
# AI出力の正規化（信用せずに1件ずつ検査する）
# ==================================================================

def clean_text(value, limit=400):
    """文字列以外・空白だけを "" に寄せ、改行を潰して長さを切る。"""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit]


def limit_sentences(text, max_sentences=2):
    """要約を最大2文に切る（C2）。

    AIが3文以上返してきた場合、こちらで切る。翻訳や言い換えはしない
    （切るだけなら事実を歪めないが、書き換えると捏造になる）。
    """
    if not text:
        return ""
    out = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in "。．.!?！？":
            out.append(buf.strip())
            buf = ""
            if len(out) >= max_sentences:
                break
    if buf.strip() and len(out) < max_sentences:
        out.append(buf.strip())
    return "".join(out)[:SUMMARY_MAX_CHARS]


def looks_speculative(text):
    """推測・評価の語を含むか（C2 の機械チェック）。

    プロンプトで禁止しているが、モデルが従わないことは普通に起きる。
    出力側でも見て、引っかかったら要約を空にする（言い換えて残そうとはしない）。
    """
    return any(marker in text for marker in SPECULATION_MARKERS)


def normalize_tags(value):
    """tags を固定12タグ集合の中だけに絞る（契約§3・要件6）。

    集合外の語（"Politics"、"IT" など）は**捨てる**。近い意味に寄せる変換はしない。
    勝手な対応表を作ると、画面のフィルタが契約と食い違う原因になる。
    """
    if not isinstance(value, list):
        return []
    out = []
    for tag in value:
        if not isinstance(tag, str):
            continue
        tag = tag.strip()
        if tag in ALLOWED_TAGS and tag not in out:
            out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def normalize_stance(value):
    """stance は support/critical/neutral のみ。それ以外・判定不能は neutral（契約§3）。"""
    if isinstance(value, str) and value.strip().lower() in ALLOWED_STANCES:
        return value.strip().lower()
    return "neutral"


def normalize_one(article, raw):
    """AIが返した1件を契約§3 の形に正規化する。使えなければ None を返す。

    None を返すと呼び出し側がその記事だけ passthrough にする（部分失敗の封じ込め）。
    「使える」の最低条件は title_ja が取れていること。訳が無ければAIを適用した意味がない。
    """
    if not isinstance(raw, dict):
        return None

    title_original = article.get("title_original") or ""
    title_ja = clean_text(raw.get("title_ja"))
    if not title_ja:
        return None

    summary_ja = limit_sentences(clean_text(raw.get("summary_ja")))
    if looks_speculative(summary_ja):
        # C2 違反の要約は載せない。空にする（書き直すと捏造になる）。
        summary_ja = ""

    stance = normalize_stance(raw.get("stance"))
    stance_reason = clean_text(raw.get("stance_reason"), limit=200)
    if not stance_reason:
        # 契約§3「必ず stance_reason（1文の根拠）を付ける」。
        # AIが根拠を返さなかった＝根拠を確認できていないので neutral に落とす。
        stance = "neutral"
        stance_reason = "根拠が得られなかったため中立とした"

    # C5：key_phrase_original は**原語のまま**。ここが比較表（契約§4 phrases）の核心。
    # 日本語に訳したものが入ってきたら比較にならないので、原語見出しに含まれる語句だけを
    # 採用し、含まれない場合は原語見出しそのものに戻す。翻訳で潰さない。
    key_original = clean_text(raw.get("key_phrase_original"), limit=200)
    if not key_original or (title_original and key_original not in title_original):
        key_original = title_original
    key_ja = clean_text(raw.get("key_phrase_ja"), limit=200)

    return {
        "article_id": article["article_id"],
        "title_ja": title_ja,
        "summary_ja": summary_ja,
        "tags": normalize_tags(raw.get("tags")),
        "stance": stance,
        "stance_reason": stance_reason,
        "key_phrase_original": key_original,
        "key_phrase_ja": key_ja,
        "enriched_by": BY_GEMINI,
    }


# ==================================================================
# プロンプト
#
# C2（事実のみ・最大2文）と C5（原語を残す）はプロンプトで**明示的に禁止／指示**する。
# 出力側の検査（normalize_one）と二重にしているのは、どちらか一方だけでは漏れるため。
#   - プロンプトだけ：モデルが従わないことがある
#   - 検査だけ：毎回大量に捨てることになり品質が落ちる
# 記事本文は渡さない。渡すのは見出しのみ（C1・C2）。
# ==================================================================

SYSTEM_RULES = """あなたは報道見出しの翻訳・分類を行う。入力は見出しのみで、記事本文は与えられない。
見出しに書かれていないことは一切出力してはならない。

各記事について次の項目を作る。
1. title_ja: 見出しの日本語訳。すでに日本語ならそのまま写す。
2. summary_ja: 見出しから読み取れる事実のみを日本語で最大2文。
   - 禁止: 評価・論評・推測・予測・形容・修飾・背景説明・見出しに無い固有名詞や数字の追加。
   - 禁止表現: 「だろう」「かもしれない」「と思われる」「と見られる」「べきだ」
     「可能性が高い」「予想される」「期待される」など断定できない言い方。
   - 見出しが短く事実を2文にできない場合は1文でよい。事実が読み取れなければ空文字 "" にする。
     推測で埋めてはならない。
3. tags: 次の12語からのみ1〜3個選ぶ。この12語以外は絶対に出力しない。
   政治, 経済, 安全保障, 外交, 気候, 人権, 科学技術, 保健, 社会, 文化, スポーツ, 災害
4. stance: この媒体が扱う対象に対する立場。support（支持・擁護） / critical（批判・非難） /
   neutral（中立・事実報道）のいずれか1語。判断できない場合は必ず neutral。
5. stance_reason: stance の根拠を日本語1文で書く。見出しの語を根拠に挙げる。
6. key_phrase_original: 見出しの中で特徴的な語句を**原語の表記のまま**抜き出す。
   絶対に翻訳・言い換え・転写をしない。見出しに現れる文字列をそのまま部分抜粋する。
   同じ出来事を国ごとにどう呼んでいるかを並べて比較するために使うので、
   ここを日本語にすると比較ができなくなる。
7. key_phrase_ja: key_phrase_original の日本語訳。

出力は JSON オブジェクトのみ。前後に説明文やコードフェンスを付けない。形式:
{"results":[{"id":"<入力のid>","title_ja":"...","summary_ja":"...","tags":["..."],
"stance":"neutral","stance_reason":"...","key_phrase_original":"...","key_phrase_ja":"..."}]}
入力の記事すべてについて、入力と同じ id を付けて1件ずつ返す。"""


def build_prompt(batch):
    """1リクエスト分のプロンプト本文を作る。渡すのは id・言語・媒体・見出しだけ。

    article_id をそのまま id として渡し、返ってきた id で突き合わせる。
    順番だけで突き合わせると、モデルが1件落としたときに全件がずれる。
    """
    lines = []
    for article in batch:
        lines.append(json.dumps({
            "id": article["article_id"],
            "lang": article.get("lang") or "unknown",
            "source": article.get("source") or "",
            "country": article.get("country") or "",
            "headline": article.get("title_original") or "",
        }, ensure_ascii=False))
    return "%s\n\n入力記事（1行1件のJSON）:\n%s" % (SYSTEM_RULES, "\n".join(lines))


def build_request_body(batch):
    """REST の :generateContent に渡すリクエストボディ。

    responseMimeType に application/json を指定して JSON で返させる（契約§3）。
    temperature=0 は、翻訳と分類で毎回違う答えが出ると差分が読めなくなるため。
    """
    return {
        "contents": [{"role": "user", "parts": [{"text": build_prompt(batch)}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "maxOutputTokens": 8192,
        },
    }


# ==================================================================
# HTTP層（urllib 直叩き。ここだけをテストでモックする）
# ==================================================================

class ApiError(Exception):
    """API呼び出しの失敗。status にHTTPコード（不明なら None）を持たせる。

    retryable=True なら指数バックオフで再試行する対象（429 / 5xx / 通信断）。
    """

    def __init__(self, message, status=None, retryable=False):
        Exception.__init__(self, message)
        self.status = status
        self.retryable = retryable


def http_post_json(url, body, api_key, timeout=TIMEOUT_SEC):
    """JSONをPOSTしてJSONを返す。失敗は ApiError に翻訳する。

    APIキーは **ヘッダ** で送る。URLクエリ（?key=...）に載せると、
    例外メッセージやログにURLが出た瞬間にキーが漏れる。
    例外メッセージにもレスポンス本文を入れない（本文にキーが echo されうる）。
    """
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "x-goog-api-key": api_key,
        "User-Agent": "WorldLensPhase1Enrich/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            status = getattr(res, "status", 200) or 200
            raw = res.read()
    except urllib.error.HTTPError as e:
        status = e.code
        # 429（レート超過）と5xx（サーバ側）だけ再試行する。
        # 400（リクエスト不正）・401/403（キーが不正）は何度送っても同じなので即諦める。
        retryable = status == 429 or 500 <= status < 600
        raise ApiError("HTTP %d" % status, status=status, retryable=retryable)
    except urllib.error.URLError as e:
        # DNS・接続断・タイムアウト。理由は型名だけ残す（文面にURLを含めない）。
        raise ApiError("URLError: %s" % type(e.reason).__name__, retryable=True)
    except Exception as e:                  # socket.timeout など想定外も落とさず翻訳する
        raise ApiError("%s" % type(e).__name__, retryable=True)

    if status != 200:
        retryable = status == 429 or 500 <= status < 600
        raise ApiError("HTTP %d" % status, status=status, retryable=retryable)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        # 本体が壊れている。再送すれば直ることもあるので retryable にする。
        raise ApiError("レスポンスがJSONではありません", status=status, retryable=True)


def extract_text(api_response):
    """generateContent のレスポンスから本文テキストを取り出す。

    candidates[0].content.parts[*].text を連結する。
    safety でブロックされた場合など parts が無い形もあるので、その場合は "" を返す
    （呼び出し側がパース失敗として扱い、そのバッチだけ passthrough になる）。
    """
    if not isinstance(api_response, dict):
        return ""
    candidates = api_response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        return ""
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def parse_results(text):
    """モデルが返したテキストから results 配列を取り出して {id: dict} にする。

    responseMimeType で JSON を要求しているが、コードフェンスや前置きが
    付いてくることがあるので、最初の { から最後の } までを切り出して読み直す。
    それでも読めなければ ValueError（呼び出し側がバッチ単位で passthrough に落とす）。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("空のレスポンス")
    raw = text.strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("JSONオブジェクトが見つかりません")
        parsed = json.loads(raw[start:end + 1])   # ここで失敗したら ValueError がそのまま伝わる

    if isinstance(parsed, list):
        results = parsed                      # results で包まずに配列だけ返す場合も受ける
    elif isinstance(parsed, dict):
        results = parsed.get("results")
        if not isinstance(results, list):
            raise ValueError("results 配列がありません")
    else:
        raise ValueError("想定外のJSON型")

    by_id = {}
    for item in results:
        if isinstance(item, dict):
            key = item.get("id") or item.get("article_id")
            if isinstance(key, str) and key:
                by_id[key] = item
    return by_id


# ==================================================================
# バッチ分割（A12：1日30リクエスト以内の担保）
# ==================================================================

def plan_batches(articles, batch_size=BATCH_SIZE, max_batches=MAX_BATCHES):
    """記事一覧をバッチに切る。本数が max_batches を超える場合は1バッチの件数を増やす。

    「1記事1リクエスト」を構造的に不可能にするのがこの関数の役目。
    記事が増えてもリクエスト本数は max_batches で頭打ちになる（＝A12 が件数に依存しない）。
      例) 240記事 / batch_size=10 → 24本 … が max_batches=20 を超えるので
          1バッチ12件に広げて20本にする。
    """
    total = len(articles)
    if total == 0:
        return []
    size = max(1, int(batch_size))
    if max_batches and max_batches > 0:
        # 切り上げ除算。size を必要なだけ大きくして本数を max_batches 以内に収める。
        needed = -(-total // max_batches)
        size = max(size, needed)
    return [articles[i:i + size] for i in range(0, total, size)]


def estimate_requests(article_count, batch_size=BATCH_SIZE, max_batches=MAX_BATCHES):
    """記事件数から通常時のリクエスト本数を求める（手順書とログに出す検算用）。"""
    return len(plan_batches([None] * article_count, batch_size, max_batches))


# ==================================================================
# 1バッチの処理（リトライ／部分失敗の封じ込め）
# ==================================================================

class Budget(object):
    """使えるHTTPリクエスト数の残量を数える小さな入れ物。

    リトライも1リクエストとして数える。Googleの RPD はリトライを区別してくれないので、
    ここで区別しないのが安全側（A12 の「1日30リクエスト以内」はリトライ込みで守る）。
    """

    def __init__(self, limit):
        self.limit = int(limit)
        self.used = 0

    def can_spend(self):
        return self.used < self.limit

    def spend(self):
        self.used += 1
        return self.used


def call_batch(batch, api_key, budget, poster=None, sleeper=None, pace=PACE_SEC):
    """1バッチをAPIに投げて {id: 生データ} を返す。失敗時は (None, 理由) を返す。

    契約§3「429/5xx は指数バックオフで最大3回。全滅したら degraded にして処理を続ける」。
    ここでは例外を外に出さない。戻り値で成否を伝え、呼び出し側が
    「このバッチだけ passthrough」を選べるようにする（他バッチを巻き込まない）。
    """
    # 既定値は「引数で束縛」ではなく「呼ばれた時にモジュールから引く」。
    # 既定引数にすると import 時に関数オブジェクトが固定され、テストからの差し替えが効かない。
    poster = poster or http_post_json
    sleeper = sleeper or _sleep

    url = "%s/%s:generateContent" % (API_BASE, MODEL)
    body = build_request_body(batch)
    last_reason = "理由不明"

    # 契約§3「指数バックオフで最大3回」= 初回1回 + 再試行3回 = 最大4回投げる。
    for attempt in range(1, MAX_RETRY + 2):
        if not budget.can_spend():
            # 残量ゼロ。投げずに諦める（上限超過で課金・停止されるより素通しの方が安全）。
            return None, "リクエスト上限 %d 到達" % budget.limit
        budget.spend()
        try:
            response = poster(url, body, api_key)
        except ApiError as e:
            last_reason = str(e)
            if not e.retryable:
                # 400系（キー不正・リクエスト不正）は再送しても同じ。即座に諦める。
                print("      再試行しない失敗: %s" % last_reason)
                return None, last_reason
            print("      失敗 (%d/%d): %s" % (attempt, MAX_RETRY + 1, last_reason))
            if attempt <= MAX_RETRY:
                wait = BACKOFF_BASE_SEC * (2 ** (attempt - 1))   # 2 → 4 → 8 秒
                print("      %.0f秒待って再試行します" % wait)
                sleeper(wait)
            continue

        text = extract_text(response)
        try:
            by_id = parse_results(text)
        except ValueError as e:
            # パース失敗。再送で直ることがあるので retryable と同じ扱いで1回だけ粘る。
            last_reason = "パース失敗: %s" % e
            print("      %s (%d/%d)" % (last_reason, attempt, MAX_RETRY + 1))
            if attempt <= MAX_RETRY:
                sleeper(BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
            continue

        if not by_id:
            last_reason = "有効な結果が0件"
            print("      %s (%d/%d)" % (last_reason, attempt, MAX_RETRY + 1))
            if attempt <= MAX_RETRY:
                sleeper(BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
            continue

        return by_id, None

    return None, last_reason


# ==================================================================
# 全体の流れ
# ==================================================================

def enrich(day_payload, date_key=None, api_key=None, batch_size=BATCH_SIZE,
           max_requests=REQUEST_BUDGET, max_batches=MAX_BATCHES,
           poster=None, sleeper=None, pace=PACE_SEC):
    """①の出力（dict）から②の出力（dict）を作る。**例外を投げない**（C9・A3・A9）。

    どんな入力・どんなAPIの壊れ方でも必ず契約§3 の形のdictを返す。
    ここで例外を投げるとサイト全体が止まるので、この関数の外に例外を出さないことを
    最優先の性質として扱う。
    """
    if date_key is None:
        date_key = (day_payload or {}).get("date") if isinstance(day_payload, dict) else None
        date_key = date_key if isinstance(date_key, str) else jst_today()
    if api_key is None:
        api_key = read_api_key()

    articles = valid_articles(day_payload)
    notes = []

    # --- 記事が無い場合 ---------------------------------------------
    # 入力が空・壊れている・全記事にURLが無い。AIを呼ぶ意味がないので0リクエストで返す。
    # degraded はキーの有無に揃える。engine はこの経路では常に passthrough なので、
    # 無条件 false にすると「engine:passthrough なのに degraded:false」という
    # 矛盾した出力になる（契約 L118/L120）。
    if not articles:
        notes.append("対象記事が0件のためAIを呼びませんでした")
        return build_payload(date_key, [], 0, not api_key, ENGINE_PASSTHROUGH, notes)

    # --- キーが無い場合（C9・A3）------------------------------------
    # 1リクエストも投げずに全件 passthrough。ここが最も頻繁に通る経路になる。
    if not api_key:
        print("  GEMINI_API_KEY が未設定です。AIを呼ばずに素通しします（degraded: true）")
        notes.append("GEMINI_API_KEY が未設定のため全記事を素通しにしました")
        return build_payload(
            date_key,
            [passthrough_article(a) for a in articles],
            0, True, ENGINE_PASSTHROUGH, notes)

    batches = plan_batches(articles, batch_size, max_batches)
    budget = Budget(max_requests)
    print("  記事 %d件 を %d バッチ（1バッチ最大%d件）で処理します。上限 %dリクエスト"
          % (len(articles), len(batches), len(batches[0]), budget.limit))

    results = []
    ok_count = 0
    failed_batches = 0

    sleep = sleeper or _sleep
    for index, batch in enumerate(batches, 1):
        # RPM 10（最終検証レポート §1-1）に当てないための間隔。
        # バッチとバッチの間にだけ入れる（1バッチ目の前と最後には待たない）。
        if index > 1:
            sleep(pace)
        print("    バッチ %d/%d (%d件)" % (index, len(batches), len(batch)))
        try:
            by_id, reason = call_batch(batch, api_key, budget, poster, sleeper, pace)
        except Exception as e:
            # call_batch は失敗を戻り値で返す約束だが、想定外の例外でも全体を止めない。
            by_id, reason = None, "想定外の例外: %s" % type(e).__name__

        if by_id is None:
            # このバッチだけ素通しにして次へ進む（契約§3「他バッチは続行」）。
            failed_batches += 1
            notes.append("バッチ%dを素通しにしました: %s" % (index, reason))
            print("      → このバッチ %d件 を素通しにします（%s）" % (len(batch), reason))
            results.extend(passthrough_article(a) for a in batch)
            continue

        # バッチは成功。ただし記事単位でさらに検査し、駄目な記事だけ落とす。
        missing = 0
        for article in batch:
            normalized = None
            try:
                normalized = normalize_one(article, by_id.get(article["article_id"]))
            except Exception:
                normalized = None     # 正規化中の想定外エラーもその1件だけに封じ込める
            if normalized is None:
                missing += 1
                results.append(passthrough_article(article))
            else:
                ok_count += 1
                results.append(normalized)
        if missing:
            notes.append("バッチ%dのうち%d件は結果が不正のため素通しにしました" % (index, missing))
            print("      %d件成功 / %d件は素通し" % (len(batch) - missing, missing))

    # 1件もAIを適用できなかった＝実質AI無しなので degraded（A9：全滅でも完走）。
    degraded = ok_count == 0
    engine = MODEL if ok_count else ENGINE_PASSTHROUGH
    if degraded:
        notes.append("AIを適用できた記事が0件のため degraded にしました")
    elif failed_batches:
        # 一部だけ失敗。degraded は立てない（大半にAIが効いている）が記録は残す（C7）。
        notes.append("%dバッチが失敗し、その分だけ素通しになりました" % failed_batches)

    print("  AI適用 %d件 / 素通し %d件 / 実リクエスト %d回"
          % (ok_count, len(results) - ok_count, budget.used))
    return build_payload(date_key, results, budget.used, degraded, engine, notes)


# ==================================================================
# メイン
# ==================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="②AI付与：days/D.json に日本語訳・要約・タグ・論調を足して days/D.enriched.json を書く")
    parser.add_argument("--date", help="対象日（YYYY-MM-DD）。既定はJSTの今日")
    parser.add_argument("--input", help="入力ファイルを直接指定（既定 data/days/D.json）")
    parser.add_argument("--output", help="出力ファイルを直接指定（既定 data/days/D.enriched.json）")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help="1リクエストにまとめる記事数（既定 %d）" % BATCH_SIZE)
    parser.add_argument("--max-requests", type=int, default=REQUEST_BUDGET,
                        help="1日に出すHTTPリクエストの上限。リトライも数える（既定 %d）" % REQUEST_BUDGET)
    parser.add_argument("--dry-run", action="store_true",
                        help="APIを呼ばず素通し出力だけを作る（キーがあっても呼ばない）")
    args = parser.parse_args(argv)

    date_key = args.date or jst_today()
    in_path = args.input or day_path_for(date_key)
    out_path = args.output or enriched_path_for(date_key)
    api_key = "" if args.dry_run else read_api_key()

    print("=" * 66)
    print("フェーズ1 ②AI付与")
    print("  対象日        : %s" % date_key)
    print("  入力          : %s" % in_path)
    print("  出力          : %s" % out_path)
    print("  モデル        : %s" % MODEL)
    # キーそのものは絶対に出さない。「あるか無いか」だけを出す。
    print("  GEMINI_API_KEY: %s" % ("設定あり" if api_key else "未設定 → 素通しモード"))
    print("  リクエスト上限: %d回/日（2.5 Flash 実測 RPD 250 の1/8以下）" % args.max_requests)
    print("=" * 66)

    day_payload = load_json(in_path)
    if day_payload is None:
        # 入力が無い＝①が走っていない。ここは「処理が成立していない」ので1を返す。
        # （AIの失敗とは違う。AIの失敗では0を返して degraded で続ける。）
        print("結果: 失敗（入力 %s が読めません。①収集を先に実行してください）" % in_path)
        return 1

    payload = enrich(
        day_payload,
        date_key=date_key,
        api_key=api_key,
        batch_size=args.batch_size,
        max_requests=args.max_requests,
    )

    # 本文系キーが1つも無いことを書き込む前に確認する（C1・A1）。
    try:
        assert_no_body_fields(payload, "enriched")
    except RuntimeError as e:
        print("結果: 失敗（%s）" % e)
        return 1

    try:
        save_json(out_path, payload)
    except OSError as e:
        print("結果: 失敗（出力を書けません: %s: %s）" % (type(e).__name__, e))
        return 1

    print("-" * 66)
    print("engine    : %s" % payload["engine"])
    print("api_calls : %d" % payload["api_calls"])
    print("degraded  : %s" % ("true" if payload["degraded"] else "false"))
    print("articles  : %d件" % len(payload["articles"]))
    for note in payload.get("notes", []):
        print("  ⚠ %s" % note)
    if payload["degraded"]:
        # 隠さない（C7）。画面上部にも「AI処理が適用されていません」が出る（契約§5-8）。
        print("結果: 完走（ただし degraded: true ＝ AI未適用。画面にその旨を表示します）")
    else:
        print("結果: 成功")
    # degraded でも 0。ここで1を返すと GitHub Actions が赤くなり、
    # 「キーが無い日は毎日失敗」に見えてしまう。完走は成功として扱う（C9）。
    return 0


if __name__ == "__main__":
    sys.exit(main())
