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
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 使うモデルの優先順。先頭が混雑（503）や日次上限のときは次のモデルに自動で切り替える。
#   無料枠のレート上限はモデルごとに別枠なので、切り替えると使える枠も増える。
#   環境変数 GEMINI_MODELS="a,b,c" で上書きできる（コードを触らずに入れ替え可能）。
#
#   選定根拠（公式 料金ページ・廃止予定ページを 2026-09-24 に確認）:
#   - 3.5 Flash-Lite / 3.8 Flash / 3.1 Flash-Lite は「無料枠: 無料」かつ提供終了日未発表。
#   - 公式が「新しいプロジェクトでは 3.5 Flash-Lite または 3.8 Flash を使用」と明記。
#   - 2.5 系は「過去に積極的に使用したユーザーに制限」＝新規キーでは拒否されうる（使わない）。
#   - 2.0 Flash は 2026-06-01 に提供終了（使えない）。
#   - 見出しの翻訳・分類は軽い処理なので、枠が大きく空きやすい Flash-Lite を先にする。
DEFAULT_MODELS = ("gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-3.1-flash-lite")
MODEL = DEFAULT_MODELS[0]   # 後方互換（ログ表示・テスト用）。実際は configured_models() を使う

# 1リクエストにまとめる記事数。
#   大きくするとリクエスト数は減るが、1件の壊れたレスポンスで巻き込む記事が増える。
BATCH_SIZE = 10

# 1日に出してよいHTTPリクエストの上限（リトライ・モデル切替も含めて数える）。
#   通常は20本前後で終わる。503が多い日に再送・切替・後回しの再挑戦を行う余裕を持たせる。
#   無料枠は1モデルあたり1日数百回あるので、60本でも十分安全側。
REQUEST_BUDGET = 60

# 通常バッチ（リトライを除く）の本数の上限。
MAX_BATCHES = 20

# --- 503（混雑）対策 ---
OVERLOAD_TRIES = 2              # 同じモデルで503が何回続いたら次のモデルに替えるか
OVERLOAD_BACKOFF_BASE_SEC = 5.0 # 5秒 → 15秒（503は数十秒続くので2・4・8秒では短すぎた）
OVERLOAD_BACKOFF_MAX_SEC = 60.0
# --- 429（分単位のレート上限）対策 ---
RATE_TRIES = 2                  # 429 は Google が指定した秒数だけ待って同じモデルで再送
RATE_DEFAULT_WAIT_SEC = 30.0
RATE_WAIT_MAX_SEC = 65.0
# --- 後回しの再挑戦 ---
# 全モデルで失敗したバッチは捨てずに後回しにし、最後にまとめて再挑戦する。
# 混雑は数分で収まることが多いので、待ってからもう一度投げる。
DEFER_ROUNDS = 2                # 何周まで再挑戦するか
DEFER_WAIT_SEC = 90.0           # 1周目は90秒、2周目は180秒待つ
DEFER_AFTER_CONSECUTIVE_FAILS = 2   # 連続2バッチ全滅したら残りは即後回し（APIが全体的に混雑中）

MAX_RETRY = OVERLOAD_TRIES - 1  # 後方互換
BACKOFF_BASE_SEC = 2.0      # パース失敗時の再送待ち
TIMEOUT_SEC = 90            # 思考OFFで応答は速くなる。詰まった接続を長く待たない

# RPM（毎分の上限）に当てないための、バッチ間の間隔。
PACE_SEC = 6.0

# 1回の実行でAI処理に使ってよい実時間（秒）。GitHub Actions の timeout-minutes: 60 より十分短く。
# 超えたら残りは素通しで書き出す（翌回・翌日に前回結果の再利用で埋まる）。
DEADLINE_SEC = 25 * 60


def configured_models(models=None):
    """使うモデルの順番を返す。引数 → 環境変数 GEMINI_MODELS → 既定 の順に採る。"""
    if models:
        items = list(models)
    else:
        env = (os.environ.get("GEMINI_MODELS") or "").strip()
        items = [m.strip() for m in env.split(",")] if env else list(DEFAULT_MODELS)
    out = []
    for m in items:
        m = (m or "").strip()
        if m.startswith("models/"):
            m = m[len("models/"):]
        if m and m not in out:
            out.append(m)
    return out or list(DEFAULT_MODELS)

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


def build_request_body(batch, model=None):
    """REST の :generateContent に渡すリクエストボディ。

    responseMimeType に application/json を指定して JSON で返させる（契約§3）。

    「思考（thinking）」は見出しの翻訳・分類には不要なのに応答が数倍遅くなり、
    混雑時の 503／タイムアウトの原因になるので最小にする（thinking_config 参照）。

    temperature: Gemini 3 系は公式が「既定値 1.0 のままにすることを強く推奨
    （下げるとループや性能低下）」としているので指定しない。
    2.x 系だけ、翻訳・分類の揺れを減らすため 0 にする。
    """
    model = model or MODEL
    config = {
        "responseMimeType": "application/json",
        "maxOutputTokens": 8192,
    }
    if not is_gemini3(model):
        config["temperature"] = 0
    thinking = thinking_config(model)
    if thinking:
        config["thinkingConfig"] = thinking
    return {
        "contents": [{"role": "user", "parts": [{"text": build_prompt(batch)}]}],
        "generationConfig": config,
    }


def is_gemini3(model):
    """Gemini 3 以降（3.x）のモデルか。"""
    name = (model or "").lower()
    if name.startswith("models/"):
        name = name[len("models/"):]
    parts = name.split("-")
    if len(parts) >= 2 and parts[0] == "gemini":
        head = parts[1].split(".")[0]
        return head.isdigit() and int(head) >= 3
    return False


def model_supports_thinking_off(model):
    """thinkingBudget=0（思考OFF）を受け付けるモデルか（2.5 Flash / Flash-Lite のみ）。

    2.5 Pro は 0 を拒否する。3.x は thinkingBudget ではなく thinkingLevel を使う
    （公式: 同じリクエストで両方を使うと 400）。
    """
    name = (model or "").lower()
    return "2.5" in name and "pro" not in name


# 思考レベル minimal に「非対応」のモデル（公式「思考」ページの表、2026-09-24 確認）。
#   gemini-3.8-flash / 3.7-flash: 低・中・高のみ（minimal を送ると 400 INVALID_ARGUMENT）
#   gemini-3.1-pro / 3-pro 系: 低・高のみ
#   この表に無い 3.x（3.5-flash-lite / 3.5-flash / 3.6-flash / 3.1-flash-lite 等）は minimal 可。
NO_MINIMAL_THINKING_PREFIXES = ("gemini-3.8-flash", "gemini-3.7-flash")


def thinking_config(model):
    """モデルに合わせた「思考を最小にする」設定。付けないほうがよい場合は None。

    - 3.x Flash / Flash-Lite: thinkingLevel=minimal（公式:「ほとんどのクエリで思考なしと一致」）
    - minimal 非対応のモデル（3.8/3.7 Flash、3.x Pro）: low
    - 2.5 Flash / Flash-Lite: thinkingBudget=0
    それでも 400 で拒否されたら、call_batch がこの設定を外して1回だけ送り直す。
    """
    if is_gemini3(model):
        name = (model or "").lower()
        if name.startswith("models/"):
            name = name[len("models/"):]
        no_minimal = "pro" in name or any(name.startswith(p) for p in NO_MINIMAL_THINKING_PREFIXES)
        return {"thinkingLevel": "low" if no_minimal else "minimal"}
    if model_supports_thinking_off(model):
        return {"thinkingBudget": 0}
    return None


# ==================================================================
# HTTP層（urllib 直叩き。ここだけをテストでモックする）
# ==================================================================

class ApiError(Exception):
    """API呼び出しの失敗。status にHTTPコード（不明なら None）を持たせる。

    retryable=True なら再試行する対象（429 / 5xx / 通信断）。
    api_status / reason / quota_id / retry_after は Google のエラー本文から
    取り出した「分類に使う短い記号」だけ。エラーの文章そのものは保持しない
    （本文にキーやリクエスト内容が echo されうるため）。
    """

    def __init__(self, message, status=None, retryable=False, api_status="",
                 reason="", quota_id="", retry_after=None):
        Exception.__init__(self, message)
        self.status = status
        self.retryable = retryable
        self.api_status = api_status or ""
        self.reason = reason or ""
        self.quota_id = quota_id or ""
        self.retry_after = retry_after


def _parse_duration(text):
    """"37s" / "1.5s" / "37" を秒（float）に。読めなければ None。"""
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    t = text.strip().lower().rstrip("s")
    try:
        return max(0.0, float(t))
    except ValueError:
        return None


def parse_error_body(raw, headers=None):
    """Google API のエラー本文から分類用の記号だけを取り出す。

    返り値: dict(api_status, reason, quota_id, retry_after)
      api_status  … "UNAVAILABLE" / "RESOURCE_EXHAUSTED" / "INVALID_ARGUMENT" など
      reason      … ErrorInfo.reason（"API_KEY_INVALID" など）
      quota_id    … QuotaFailure の quotaId（"...PerDay..." なら日次上限）
      retry_after … RetryInfo.retryDelay か Retry-After ヘッダ（秒）
    """
    info = {"api_status": "", "reason": "", "quota_id": "", "retry_after": None}
    if headers is not None:
        try:
            info["retry_after"] = _parse_duration(headers.get("Retry-After"))
        except Exception:
            pass
    try:
        doc = json.loads((raw or b"").decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return info
    if isinstance(doc, list) and doc:
        doc = doc[0]
    err = doc.get("error") if isinstance(doc, dict) else None
    if not isinstance(err, dict):
        return info
    if isinstance(err.get("status"), str):
        info["api_status"] = err["status"]
    for d in err.get("details") or []:
        if not isinstance(d, dict):
            continue
        kind = str(d.get("@type", ""))
        if kind.endswith("ErrorInfo") and isinstance(d.get("reason"), str):
            info["reason"] = d["reason"]
        elif kind.endswith("RetryInfo"):
            delay = _parse_duration(d.get("retryDelay"))
            if delay is not None:
                info["retry_after"] = delay
        elif kind.endswith("QuotaFailure"):
            for v in d.get("violations") or []:
                if isinstance(v, dict) and isinstance(v.get("quotaId"), str):
                    info["quota_id"] = v["quotaId"]
                    break
    return info


def _api_error_from_http(status, raw, headers):
    info = parse_error_body(raw, headers)
    label = "HTTP %d" % status
    if info["api_status"]:
        label += " %s" % info["api_status"]
    if info["reason"]:
        label += " (%s)" % info["reason"]
    return ApiError(label, status=status,
                    retryable=(status == 429 or 500 <= status < 600), **info)


def http_post_json(url, body, api_key, timeout=None):
    """JSONをPOSTしてJSONを返す。失敗は ApiError に翻訳する。

    APIキーは **ヘッダ** で送る。URLクエリ（?key=...）に載せると、
    例外メッセージやログにURLが出た瞬間にキーが漏れる。
    例外メッセージにもレスポンス本文を入れない（本文にキーが echo されうる）。
    """
    if timeout is None:
        timeout = TIMEOUT_SEC
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "x-goog-api-key": api_key,
        "User-Agent": "WorldLensPhase1Enrich/1.1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            status = getattr(res, "status", 200) or 200
            raw = res.read()
    except urllib.error.HTTPError as e:
        try:
            raw_err = e.read(65536)
        except Exception:
            raw_err = b""
        raise _api_error_from_http(e.code, raw_err, getattr(e, "headers", None))
    except urllib.error.URLError as e:
        # DNS・接続断・タイムアウト。理由は型名だけ残す（文面にURLを含めない）。
        raise ApiError("URLError: %s" % type(e.reason).__name__, retryable=True)
    except Exception as e:                  # socket.timeout など想定外も落とさず翻訳する
        raise ApiError("%s" % type(e).__name__, retryable=True)

    if status != 200:
        raise _api_error_from_http(status, raw, None)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        # 本体が壊れている。再送すれば直ることもあるので retryable にする。
        raise ApiError("レスポンスがJSONではありません", status=status, retryable=True)


# エラーの分類（call_batch がこれを見て「待つ／モデルを替える／諦める」を決める）
ERR_FATAL = "fatal"          # キー不正・権限なし → 何をしても無駄。全バッチ中止
ERR_MODEL_GONE = "model"     # そのモデルが使えない（404・日次上限） → 次のモデルへ
ERR_OVERLOAD = "overload"    # 503/500/504/通信断 → 少し待って再送、駄目なら次のモデルへ
ERR_RATE = "rate"            # 429 分単位の上限 → 指定秒数だけ待って同じモデルで再送
ERR_BAD_REQUEST = "bad"      # 400 その他 → このバッチの中身が悪い。再送しない

AUTH_REASONS = ("API_KEY_INVALID", "API_KEY_SERVICE_BLOCKED",
                "SERVICE_DISABLED", "CONSUMER_INVALID", "API_KEY_HTTP_REFERRER_BLOCKED",
                "API_KEY_IP_ADDRESS_BLOCKED")


def classify_error(e):
    """ApiError を上の5分類のどれかにする。"""
    status = e.status
    reason = (e.reason or "").upper()
    api_status = (e.api_status or "").upper()
    if status == 401 or reason in AUTH_REASONS or api_status == "UNAUTHENTICATED":
        return ERR_FATAL
    if status == 403 or api_status == "PERMISSION_DENIED":
        # キー自体の問題（API_KEY_* 等）は上で FATAL 済み。ここに来るのは
        # 「このモデルはあなたのプロジェクトでは使えない」型（2.5系の新規利用制限など）。
        # 他のモデルなら通るかもしれないので、次のモデルへ回す。
        return ERR_MODEL_GONE
    if status == 400 and api_status == "FAILED_PRECONDITION":
        # 「この地域では無料枠が使えない」等。キー単位の問題なので全体を止める。
        return ERR_FATAL
    if status == 404:
        return ERR_MODEL_GONE
    if status == 429:
        quota = (e.quota_id or "").lower()
        if "perday" in quota or "per_day" in quota or "daily" in quota:
            return ERR_MODEL_GONE          # 今日はこのモデルはもう使えない
        return ERR_RATE
    if status is not None and 400 <= status < 500:
        return ERR_BAD_REQUEST
    if e.retryable or status is None or (status is not None and status >= 500):
        return ERR_OVERLOAD
    return ERR_BAD_REQUEST


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
# 1バッチの処理（リトライ／モデル切替／部分失敗の封じ込め）
# ==================================================================

class Budget(object):
    """使えるHTTPリクエスト数の残量を数える小さな入れ物。

    リトライも1リクエストとして数える。Googleの RPD はリトライを区別してくれないので、
    ここで区別しないのが安全側。
    """

    def __init__(self, limit, deadline_sec=None, clock=None):
        self.limit = int(limit)
        self.used = 0
        self._clock = clock or time.monotonic
        self._deadline = (self._clock() + deadline_sec) if deadline_sec else None

    def out_of_time(self):
        return self._deadline is not None and self._clock() >= self._deadline

    def can_spend(self):
        return self.used < self.limit and not self.out_of_time()

    def spend(self):
        self.used += 1
        return self.used


class ModelPool(object):
    """使うモデルの順番と「今日はもう使えないモデル」を覚えておく入れ物。

    * 503 が続いたモデルは列の最後に回す（次のバッチからは空いているモデルを先に試す）
    * 404・日次上限のモデルは今日は二度と使わない
    * キー不正などが出たら fatal を立て、残りバッチは1リクエストも出さない
    無料枠のレート上限はモデルごとに別枠なので、切り替えれば枠も増える。
    """

    def __init__(self, models):
        seen = []
        for m in models or ():
            m = (m or "").strip()
            if m and m not in seen:
                seen.append(m)
        self.order = seen or [DEFAULT_MODELS[0]]
        self.dead = set()
        self.fatal_reason = ""
        self.served = {}          # モデル名 → 成功したバッチ数
        self.last_kind = ""       # 直前の call_batch の失敗の種類（後回しにするか判断用）

    def alive(self):
        return [m for m in self.order if m not in self.dead]

    def kill(self, model):
        self.dead.add(model)

    def demote(self, model):
        if model in self.order and len(self.order) > 1:
            self.order.remove(model)
            self.order.append(model)

    def record_success(self, model):
        self.served[model] = self.served.get(model, 0) + 1

    def top_model(self):
        if not self.served:
            return None
        # 同数ならリストの先頭側（優先度が高いモデル）を採る
        return sorted(self.served.items(),
                      key=lambda kv: (-kv[1], self.order.index(kv[0])
                                      if kv[0] in self.order else 99))[0][0]


def _wait_for_overload(attempt):
    """503 等の待ち時間。5 → 15 → 45 秒（混雑は数十秒〜数分続くので短すぎると無意味）。"""
    return min(OVERLOAD_BACKOFF_MAX_SEC, OVERLOAD_BACKOFF_BASE_SEC * (3 ** (attempt - 1)))


def call_batch(batch, api_key, budget, poster=None, sleeper=None, pace=PACE_SEC, pool=None):
    """1バッチをAPIに投げて (by_id, 失敗理由, 使ったモデル) を返す。

    失敗時は (None, 理由, None)。例外は外に出さない。
    エラーの種類で振る舞いを変える（classify_error）:
      overload(503等) … 待って同じモデルで再送 → OVERLOAD_TRIES 回駄目なら次のモデルへ
      rate(429/分)     … Googleが指定した秒数だけ待って同じモデルで再送
      model(404/日次)  … そのモデルを今日は捨てて次のモデルへ
      fatal(キー不正)  … 全体を中止（残りバッチも投げない）
      bad(400)         … このバッチだけ諦める
    """
    poster = poster or http_post_json
    sleeper = sleeper or _sleep
    if pool is None:
        pool = ModelPool(configured_models())
    pool.last_kind = ""
    body_cache = {}
    last_reason = "理由不明"
    parse_only = True           # 全モデルで「返事は来たが中身が壊れていた」だけか

    for model in list(pool.alive()):
        if pool.fatal_reason:
            break
        if model in pool.dead:
            continue
        url = "%s/%s:generateContent" % (API_BASE, model)
        if model not in body_cache:
            body_cache[model] = build_request_body(batch, model)
        body = body_cache[model]
        overload_tries = 0
        rate_tries = 0
        parse_tries = 0

        while True:
            if not budget.can_spend():
                return None, "リクエスト上限 %d 到達" % budget.limit, None
            budget.spend()
            try:
                response = poster(url, body, api_key)
            except ApiError as e:
                kind = classify_error(e)
                parse_only = False
                last_reason = "%s: %s" % (model, e)
                if kind == ERR_FATAL:
                    pool.fatal_reason = str(e)
                    print("      中止: %s（キー・権限の問題。残りも投げません）" % last_reason)
                    return None, last_reason, None
                if kind == ERR_BAD_REQUEST and "thinkingConfig" in body["generationConfig"]:
                    # 思考設定の書式をモデルが受け付けなかった可能性。外して1回だけ送り直す。
                    body = dict(body)
                    body["generationConfig"] = {k: v for k, v in body["generationConfig"].items()
                                                if k != "thinkingConfig"}
                    body_cache[model] = body
                    print("      %s: 400 のため思考設定を外して再送" % model)
                    continue
                if kind == ERR_BAD_REQUEST:
                    print("      再試行しない失敗: %s" % last_reason)
                    pool.last_kind = ERR_BAD_REQUEST
                    return None, last_reason, None
                if kind == ERR_MODEL_GONE:
                    print("      %s は今日は使えません（%s）→ 次のモデルへ" % (model, e))
                    pool.kill(model)
                    break
                if kind == ERR_RATE:
                    rate_tries += 1
                    if rate_tries > RATE_TRIES:
                        print("      %s: 429 が続くので次のモデルへ" % model)
                        break
                    wait = e.retry_after if e.retry_after is not None else RATE_DEFAULT_WAIT_SEC
                    wait = min(RATE_WAIT_MAX_SEC, max(1.0, wait + 1.0))
                    print("      %s: 429 分単位の上限。%.0f秒待って再送" % (model, wait))
                    sleeper(wait)
                    continue
                # ERR_OVERLOAD
                overload_tries += 1
                if overload_tries >= OVERLOAD_TRIES:
                    print("      %s: 混雑（%s）が続くので次のモデルへ" % (model, e))
                    pool.demote(model)
                    break
                wait = e.retry_after if e.retry_after is not None else _wait_for_overload(overload_tries)
                wait = min(OVERLOAD_BACKOFF_MAX_SEC, wait)
                print("      %s: %s → %.0f秒待って再送 (%d/%d)"
                      % (model, e, wait, overload_tries, OVERLOAD_TRIES))
                sleeper(wait)
                continue

            text = extract_text(response)
            try:
                by_id = parse_results(text)
            except ValueError as e:
                by_id = None
                last_reason = "%s: パース失敗: %s" % (model, e)
            else:
                if not by_id:
                    last_reason = "%s: 有効な結果が0件" % model
            if by_id:
                pool.record_success(model)
                return by_id, None, model
            parse_tries += 1
            print("      %s (%d/2)" % (last_reason, parse_tries))
            if parse_tries >= 2:
                break                # 同じモデルで2回壊れたら次のモデルへ
            sleeper(BACKOFF_BASE_SEC)

    if pool.fatal_reason:
        return None, "中止: %s" % pool.fatal_reason, None
    if parse_only and last_reason != "理由不明":
        pool.last_kind = ERR_BAD_REQUEST     # 中身の問題。待っても直らないので後回しにしない
    return None, last_reason, None


# ==================================================================
# 全体の流れ
# ==================================================================

def reusable_previous(previous, articles):
    """前回の出力（同じ日の .enriched.json）から、AI適用済みの記事だけを取り出す。

    daily.yml は1日2回走る。1回目で翻訳できた記事を2回目で再送すると、
    枠を無駄にするうえ、2回目が 503 で全滅したときに1回目の成果を素通しで上書きしてしまう。
    ここで拾った記事は API に投げずにそのまま使う。見出しが変わった記事は使わない
    （article_id が同じでも key_phrase_original が見出しに含まれない＝別物とみなす）。
    """
    if not isinstance(previous, dict):
        return {}
    by_id = {}
    for art in previous.get("articles") or []:
        if not isinstance(art, dict):
            continue
        if art.get("enriched_by") != BY_GEMINI:
            continue
        if not all(k in art for k in ARTICLE_FIELDS):
            continue
        by_id[art.get("article_id")] = art
    out = {}
    for a in articles:
        old = by_id.get(a["article_id"])
        if old is None:
            continue
        phrase = old.get("key_phrase_original") or ""
        title = a.get("title_original") or ""
        if phrase and phrase not in title:
            continue
        out[a["article_id"]] = dict((k, old[k]) for k in ARTICLE_FIELDS)
    return out


def _apply_batch_result(batch, by_id, done):
    """成功したバッチの結果を記事単位で検査して done に入れる。不正だった件数を返す。"""
    missing = 0
    for article in batch:
        try:
            normalized = normalize_one(article, by_id.get(article["article_id"]))
        except Exception:
            normalized = None     # 正規化中の想定外エラーもその1件だけに封じ込める
        if normalized is None:
            missing += 1
        else:
            done[article["article_id"]] = normalized
    return missing


def enrich(day_payload, date_key=None, api_key=None, batch_size=BATCH_SIZE,
           max_requests=REQUEST_BUDGET, max_batches=MAX_BATCHES,
           poster=None, sleeper=None, pace=PACE_SEC, models=None, previous=None,
           defer_rounds=None, defer_wait=None, deadline_sec=DEADLINE_SEC):
    """①の出力（dict）から②の出力（dict）を作る。**例外を投げない**（C9・A3・A9）。

    503 対策の流れ:
      1. 前回の出力でAI適用済みの記事は再利用（APIに投げない）
      2. 残りをバッチに分け、モデルを自動で切り替えながら投げる
      3. 失敗したバッチは「後回し」にして、しばらく待ってからもう一度まとめて投げる
      4. それでも駄目な記事だけ passthrough
    """
    if date_key is None:
        date_key = (day_payload or {}).get("date") if isinstance(day_payload, dict) else None
        date_key = date_key if isinstance(date_key, str) else jst_today()
    if api_key is None:
        api_key = read_api_key()
    if defer_rounds is None:
        defer_rounds = DEFER_ROUNDS
    if defer_wait is None:
        defer_wait = DEFER_WAIT_SEC

    articles = valid_articles(day_payload)
    notes = []

    if not articles:
        notes.append("対象記事が0件のためAIを呼びませんでした")
        return build_payload(date_key, [], 0, not api_key, ENGINE_PASSTHROUGH, notes)

    reused = reusable_previous(previous, articles)

    if not api_key:
        if reused:
            # キーが消えても、前回までに翻訳できた分は捨てない。
            print("  GEMINI_API_KEY が未設定です。前回の翻訳 %d件 だけ再利用します" % len(reused))
            notes.append("GEMINI_API_KEY が未設定のため、新しい記事は素通しにしました"
                         "（前回の翻訳 %d件 は再利用）" % len(reused))
            out = [reused.get(a["article_id"]) or passthrough_article(a) for a in articles]
            return build_payload(date_key, out, 0, False, configured_models(models)[0], notes)
        print("  GEMINI_API_KEY が未設定です。AIを呼ばずに素通しします（degraded: true）")
        notes.append("GEMINI_API_KEY が未設定のため全記事を素通しにしました")
        return build_payload(
            date_key,
            [passthrough_article(a) for a in articles],
            0, True, ENGINE_PASSTHROUGH, notes)

    done = dict(reused)
    todo = [a for a in articles if a["article_id"] not in done]
    if reused:
        print("  前回の出力から %d件 を再利用（APIに投げません）" % len(reused))
        notes.append("前回の実行でAI適用済みの %d件 を再利用しました" % len(reused))

    pool = ModelPool(configured_models(models))
    budget = Budget(max_requests, deadline_sec)
    sleep = sleeper or _sleep
    batches = plan_batches(todo, batch_size, max_batches)
    if batches:
        print("  記事 %d件 を %d バッチ（1バッチ最大%d件）で処理します。上限 %dリクエスト"
              % (len(todo), len(batches), len(batches[0]), budget.limit))
        print("  モデルの順番: %s" % " → ".join(pool.order))

    invalid_items = 0
    stop_reason = ""
    given_up = []               # 待っても直らない失敗（400・中身が壊れている）
    pending = list(batches)
    first_call = True

    # round 0 = 通常の1周目、round 1.. = 後回しにしたバッチの再挑戦
    for rnd in range(0, defer_rounds + 1):
        if not pending or stop_reason:
            break
        if rnd > 0:
            if budget.out_of_time() or not budget.can_spend():
                stop_reason = "リクエスト上限または実行時間の上限に到達"
                break
            wait = defer_wait * rnd
            print("  後回しにした %d バッチを %.0f秒 待ってから再挑戦します（%d/%d回目）"
                  % (len(pending), wait, rnd, defer_rounds))
            sleep(wait)
            # 混雑でモデルの順番が入れ替わっていたら元の優先順に戻す（日次上限のモデルは除外のまま）
            pool.order = [m for m in configured_models(models)]
        deferred = []
        consecutive_fail = 0
        for index, batch in enumerate(pending, 1):
            if stop_reason:
                deferred.append(batch)
                continue
            if consecutive_fail >= DEFER_AFTER_CONSECUTIVE_FAILS:
                # 2バッチ続けて全モデル失敗＝今は API 全体が混んでいる。
                # 残りは投げずに後回しにする（枠と時間を無駄にしない）。
                deferred.append(batch)
                continue
            if not first_call:
                sleep(pace)
            first_call = False
            print("    %sバッチ %d/%d (%d件)" % ("再挑戦 " if rnd else "", index, len(pending), len(batch)))
            try:
                by_id, reason, model = call_batch(batch, api_key, budget, poster, sleeper, pace, pool)
            except Exception as e:
                by_id, reason, model = None, "想定外の例外: %s" % type(e).__name__, None

            if by_id is None:
                if pool.last_kind == ERR_BAD_REQUEST:
                    given_up.append(batch)
                    print("      → このバッチは素通し（%s）" % reason)
                    continue
                consecutive_fail += 1
                deferred.append(batch)
                if not pool.fatal_reason:
                    print("      → 後回し（%s）" % reason)
                if pool.fatal_reason:
                    stop_reason = reason
                elif budget.out_of_time():
                    stop_reason = "実行時間の上限 %d分 到達" % (deadline_sec // 60)
                elif not budget.can_spend():
                    stop_reason = "リクエスト上限 %d 到達" % budget.limit
                elif not pool.alive():
                    stop_reason = "使えるモデルが残っていません（%s）" % reason
                continue
            consecutive_fail = 0
            missing = _apply_batch_result(batch, by_id, done)
            if missing:
                invalid_items += missing
                print("      %s: %d件成功 / %d件は結果が不正" % (model, len(batch) - missing, missing))
        pending = deferred
        if pending and rnd == defer_rounds and not stop_reason:
            stop_reason = "後回しの再挑戦 %d回 でも混雑が解消しませんでした" % defer_rounds

    results = [done.get(a["article_id"]) or passthrough_article(a) for a in articles]
    ok_count = sum(1 for a in articles if a["article_id"] in done)
    failed_articles = sum(len(b) for b in pending)
    bad_articles = sum(len(b) for b in given_up)

    if failed_articles:
        notes.append("%d件を素通しにしました: %s" % (failed_articles, stop_reason or "理由不明"))
    if bad_articles:
        notes.append("%d件はAPIが受け付けなかったため素通しにしました（再試行しても直らない種類の失敗）"
                     % bad_articles)
    if invalid_items:
        notes.append("%d件はAIの結果が不正だったため素通しにしました" % invalid_items)
    if len(pool.served) > 1 or (pool.served and pool.top_model() != configured_models(models)[0]):
        notes.append("使ったモデル: %s" % ", ".join(
            "%s×%d" % kv for kv in sorted(pool.served.items())))

    degraded = ok_count == 0
    if degraded:
        engine = ENGINE_PASSTHROUGH
        notes.append("AIを適用できた記事が0件のため degraded にしました")
    else:
        engine = pool.top_model() or configured_models(models)[0]

    print("  AI適用 %d件（うち再利用 %d件）/ 素通し %d件 / 実リクエスト %d回"
          % (ok_count, len(reused), len(results) - ok_count, budget.used))
    return build_payload(date_key, results, budget.used, degraded, engine, notes)


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
    parser.add_argument("--models",
                        help="使うモデルをカンマ区切りで指定（既定 %s。環境変数 GEMINI_MODELS でも可）"
                        % ",".join(DEFAULT_MODELS))
    parser.add_argument("--fresh", action="store_true",
                        help="前回の翻訳結果を再利用せず、全記事をAPIに投げ直す")
    args = parser.parse_args(argv)
    models = configured_models(args.models.split(",") if args.models else None)

    date_key = args.date or jst_today()
    in_path = args.input or day_path_for(date_key)
    out_path = args.output or enriched_path_for(date_key)
    api_key = "" if args.dry_run else read_api_key()

    print("=" * 66)
    print("フェーズ1 ②AI付与")
    print("  対象日        : %s" % date_key)
    print("  入力          : %s" % in_path)
    print("  出力          : %s" % out_path)
    print("  モデル        : %s（混雑時は左から順に自動切替）" % " → ".join(models))
    # キーそのものは絶対に出さない。「あるか無いか」だけを出す。
    print("  GEMINI_API_KEY: %s" % ("設定あり" if api_key else "未設定 → 素通しモード"))
    print("  リクエスト上限: %d回/回（リトライ・モデル切替・再挑戦を含む）" % args.max_requests)
    print("=" * 66)

    day_payload = load_json(in_path)
    if day_payload is None:
        # 入力が無い＝①が走っていない。ここは「処理が成立していない」ので1を返す。
        # （AIの失敗とは違う。AIの失敗では0を返して degraded で続ける。）
        print("結果: 失敗（入力 %s が読めません。①収集を先に実行してください）" % in_path)
        return 1

    # 同じ日の前回出力（1日2回走るうちの1回目など）。AI適用済みの記事は再送しない。
    previous = None if args.fresh else load_json(out_path)

    payload = enrich(
        day_payload,
        date_key=date_key,
        api_key=api_key,
        batch_size=args.batch_size,
        max_requests=args.max_requests,
        models=models,
        previous=previous,
    )

    # 今回が全滅（degraded）でも、前回のほうがAI適用件数が多ければ前回を残す。
    # 503 の日に、朝の成功を夜の失敗で上書きしないための保険。
    if previous and not args.fresh and payload.get("degraded"):
        prev_ok = sum(1 for a in previous.get("articles") or []
                      if isinstance(a, dict) and a.get("enriched_by") == BY_GEMINI)
        if prev_ok > 0:
            print("  今回はAI適用0件でしたが、前回の出力（AI適用 %d件）を残します" % prev_ok)
            print("結果: 完走（前回の出力を維持）")
            return 0

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
