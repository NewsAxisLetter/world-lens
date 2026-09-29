#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ1 ①収集スクリプト ── 12カ国の複数媒体からRSSを集めて `data/days/D.json` を書く。

フェーズ0（NHK1本）からの発展点だけを書く。phase0/scripts/fetch.py は変更しない。
契約書 `phase1/データ契約.md` §1（feeds.json）§2（days/D.json・schema_version 3）を実装する。

フェーズ0との差分
-----------------
1. フィード定義を **コード内定数から `phase1/feeds.json` に外出し**（契約§1）。
   媒体を足す・止めるのがJSON編集だけで済み、スクリプトを触らなくてよくなる。
2. **26本を順次取得**するので、1本の失敗で全体が止まらないようにした（A8）。
   各フィードを個別に try で囲み、失敗は `status:"failed"` として記録して次へ進む。
3. **レート配慮**（本タスク要件9）。
   - 同一ホストへの連続アクセスを避けるため、取得順をホストで**インターリーブ**する。
   - それでも同一ホストに戻ってきたときは最低間隔 `PER_HOST_MIN_INTERVAL_SEC` を空ける。
   - フィード間には常に `INTER_FEED_WAIT_SEC` の間隔を入れる。
   - User-Agent に連絡先URLを入れて名乗る。タイムアウトも必ず設定する。
4. `article_id = sha256(url).hexdigest()[:8]` を付与（契約§2）。②③④はこのIDで記事を参照する。
   URLが同じなら同じIDになるので、同日2回実行しても件数が増えない（A2）。
5. `rank_in_feed`（フィード内の並び順・1始まり）を付与。「そのフィードが何を上に置いたか」
   は編集判断そのもので、③のクラスタや④の表示で使うため取得時点で残す。
6. `schema_version` を 3 に上げた。

引き継いだ設計（変えていない部分）
--------------------------------
* 標準ライブラリのみ（C8）。`pip install` は不要。urllib + xml.etree だけで動く。
* 保存フィールドはホワイトリスト方式。RSSに何が入っていても
  `ALLOWED_ARTICLE_FIELDS` に無いキーは JSON に出さない（C1・A1）。
  `<description>` `<content:encoded>` `<summary>` は**読み取り自体をしない**。
* 鮮度監視は「最新記事の日付」で行う（本タスク要件7）。
  件数を数える監視では VOA World（20件返すが1年半前で凍結）や
  人民網（100件返すが1年3か月前で凍結）を永久に検出できない。実測でも確認済み。
  status は ok / stale / empty / failed の4分類。
* 月別シャード構成（data/index.json + data/months/YYYY-MM.json + data/days/D.json）。
  毎日書き換わるファイルのサイズが日数に比例しないので git 履歴が膨らまない。

★名前空間バグの回帰防止（A7）
  `findall(".//item")` は名前空間付きのRDF／Atomに **1件もマッチしない**
  （タグ実体が "{http://purl.org/rss/1.0/}item" になるため）。
  これが朝日・DW・GOV.UK を「HTTP 200 なのに0件」にしていた原因。
  本実装は必ず `local_name()` でローカル名だけを見て比較する。
  回帰テストは scripts/test_fetch.py + scripts/testdata/*.xml（ネット接続不要）。

使い方
------
  python3 scripts/fetch.py                        # 全フィードをネットから取得
  python3 scripts/fetch.py --only jp-nhk,gb-bbc   # feed_id を絞って取得（動作確認用）
  python3 scripts/fetch.py --limit 3              # 先頭3本だけ（動作確認用）
  python3 scripts/fetch.py --date 2026-09-15      # 保存先の日付を明示（通常は指定しない）
  python3 scripts/fetch.py --dry-run              # 取得するが保存しない
  python3 scripts/fetch.py --offline DIR          # DIR内の <feed_id>.xml から読む（オフライン）

終了コード
----------
  0 = 1本以上のフィードから記事が取れて保存できた（一部 stale / failed でも0）
  1 = 全フィードが失敗 or 記事0件（月別シャードに記録済み。Actionsを赤くして気付かせる）
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

SCHEMA_VERSION = 3          # 契約§2。2 = フェーズ0（月別シャード）/ 3 = 本実装（多媒体＋article_id）

# ------------------------------------------------------------------
# 出力フィールドのホワイトリスト（契約§2・C1・A1）
# ------------------------------------------------------------------
# 契約§2 のJSON例に列挙されているキーをそのまま写したもの。ここに無いキーは出さない。
#
# ※契約§2の本文は「許可フィールドはこの13キーのみ」と書いているが、直前のJSON例に
#   実際に並んでいるキーは以下の11個である（数が合わない）。機械的に検証できる
#   「列挙されたJSON例」を正とした。②③（enrich/cluster）が既に読んでいる
#   fixtures も同じ11キーで、下流と一致する方を選んでいる。
#   13に増やす判断が必要なら、増やすキー名を契約側に明記してからここに足す。
ALLOWED_ARTICLE_FIELDS = (
    "article_id",       # sha256(url) の先頭8桁。②③④が記事を参照する唯一のキー
    "feed_id",          # どのフィードから来たか（feeds.json の feed_id）
    "source",           # 媒体名
    "country",          # ISO 3166-1 alpha-2
    "media_type",       # state / public / private / independent
    "lang",             # 見出しの言語
    "title_original",   # 原文見出し（C5：必ず原語のまま残す）
    "url",              # 原文へのリンク（C4：必須）
    "published_at",     # 媒体が示した公開時刻（UTC正規化。読めなければ None）
    "fetched_at",       # こちらが取得した時刻（UTC）
    "rank_in_feed",     # フィード内の並び順（1始まり）
    "lean",             # フィード由来の論調（right/center/left/na）。無ければ "na"
    "gov_axis",         # フィード由来の対政権軸（pro_gov/indep/anti_gov/na）。無ければ "na"
)

# 記事本文が入りうるキー名。1つでも出力に現れたら設計違反として実行時に止める。
# 小文字化して比較するので "Content" "CONTENT_ENCODED" なども捕まる。
FORBIDDEN_FIELDS = (
    "body", "bodytext", "body_text", "body_html",
    "content", "content_encoded", "contentencoded", "encoded",
    "description", "summary", "text", "fulltext", "full_text", "abstract",
    "excerpt", "articlebody", "article_body",
    "snippet", "teaser", "lead", "lede", "caption", "transcript",
)
# 上のうち「本文系キー」の判定に使う部分一致パターン。
# content:encoded → "content_encoded" のように区切りを変えて逃げるのを防ぐため、
# キー名を英数字だけに正規化してから完全一致・部分一致の両方で見る。
FORBIDDEN_SUBSTRINGS = (
    "body", "content", "encoded", "description", "summary",
    "fulltext", "abstract", "excerpt", "snippet", "teaser", "transcript",
)
# 上の部分一致で誤検出してしまう正当なキー名の例外リスト。
# 例: "countries" は "content" を含まないので不要だが、将来 "summary_ja" のような
#     ②の出力キーをこのモジュールで検査したくなったときに使う口を残しておく。
FORBIDDEN_ALLOWLIST = ()

# FORBIDDEN_FIELDS を英数字だけに正規化した集合（is_forbidden_key で使う）。
_FORBIDDEN_NORMALIZED = frozenset(
    "".join(ch for ch in name if ch.isalnum()) for name in FORBIDDEN_FIELDS)

# ------------------------------------------------------------------
# 取得の設定（本タスク要件9：レート配慮）
# ------------------------------------------------------------------
# 誰が何のために取得しているか分かる形で名乗る。ブロックされたときに
# 媒体側から連絡してもらえる余地を残すため（robots/礼儀の観点）。
# ★手順書のステップで "your-name" を自分のGitHubユーザー名に書き換えてもらう。
USER_AGENT = (
    "WorldLensPhase1/1.0 (non-commercial news headline aggregator; "
    "+https://github.com/your-name/world-lens)"
)

TIMEOUT_SEC = 20                    # 1リクエストあたりの上限。無限に待たない（A8）
MAX_RETRY = 3                       # 1フィードあたり最大3回試す
RETRY_WAIT_SEC = 5                  # 5秒 → 10秒 と伸ばして打ち切り

# 同一ホストに続けて叩かない。取得順のインターリーブで基本的に避けるが、
# 同じホストに戻ってきた場合はここで最低間隔を強制する。
# 6秒は「1ホストあたり毎分10リクエスト以下」に相当し、26本を1日1回読む用途としては
# 十分に控えめ。BBCのように複数フィードを持つホストでも合計2〜3本なので影響は小さい。
PER_HOST_MIN_INTERVAL_SEC = 6.0
# フィード間の一律待ち時間。26本 × 1.0秒 ≒ 26秒で、GitHub Actions の実行時間に収まる。
INTER_FEED_WAIT_SEC = 1.0

# 鮮度監視の既定閾値（日）。feeds.json 側の stale_after_days で媒体ごとに上書きできる。
DEFAULT_STALE_AFTER_DAYS = 7

# 1フィードから取り込む記事の上限。人民網のように100件返すフィードがあり、
# 全件入れると1日のJSONが膨らむうえ③のクラスタ計算も重くなる。
# 「そのフィードが上位に置いた記事」を見たいので先頭から取る。
MAX_ITEMS_PER_FEED = 40

JST = timezone(timedelta(hours=9), "JST")

# このファイルの1つ上（= phase1/）を基準にする。どこから実行しても場所がズレない。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDS_PATH = os.path.join(ROOT, "feeds.json")
DATA_DIR = os.path.join(ROOT, "data")
INDEX_PATH = os.path.join(DATA_DIR, "index.json")
MONTHS_DIR = os.path.join(DATA_DIR, "months")
DAYS_DIR = os.path.join(DATA_DIR, "days")

# フィード状態。index.html の表示分岐と1対1で対応させる（契約§2）。
STATUS_OK = "ok"            # 取得でき、最新記事も新しい
STATUS_STALE = "stale"      # 取得できたが最新記事が古い（更新停止の疑い）
STATUS_EMPTY = "empty"      # HTTP 200 だが記事0件（配信形式変更の疑い）
STATUS_FAILED = "failed"    # 取得できなかった / XMLとして読めなかった

# 契約§1で定めた列挙値。feeds.json の検証に使う。
# 契約§1（データ契約.md 44行目）の enum に厳密に合わせる。
# 以前ここだけ "independent" と書かれており、feeds.json の "indep" を
# 不正値として弾いていた（load_feeds が ValueError）。正は契約側の "indep"。
VALID_MEDIA_TYPES = ("state", "public", "private", "indep")

# 契約§1 の任意フィールド（論調2軸と、その根拠）。欠落は許容し、あれば列挙を検証する。
VALID_LEANS = ("right", "center", "left", "na")
VALID_GOV_AXES = ("pro_gov", "indep", "anti_gov", "na")
VALID_CONFIDENCES = ("high", "medium", "low")
VALID_ROLES = ("primary", "backup")
LABEL_NA = "na"

# キー名の別名 → 正規形。分類作業を複数人・複数AIでやると表記が揺れるため、
# 読み込み時に1か所で寄せる（下流は正規形だけを見ればよい）。
FEED_KEY_ALIASES = {
    "gov_stance": "gov_axis",
    "gov_position": "gov_axis",
    "government_axis": "gov_axis",
    "political_lean": "lean",
    "leaning": "lean",
    "lean_reason": "lean_basis",
    "lean_rationale": "lean_basis",
    "basis": "lean_basis",
    "gov_stance_basis": "gov_axis_basis",
    "gov_stance_reason": "gov_axis_basis",
    "gov_axis_reason": "gov_axis_basis",
    "gov_reason": "gov_axis_basis",
    "classified_on": "classified_at",
    "classification_date": "classified_at",
    "source_urls": "sources",
    "evidence": "sources",
    "references": "sources",
    "feed_role": "role",
}

# 値の別名。比較は小文字化し、空白・ハイフンを "_" に寄せてから行う。
# center-left / center-right は「中道」ではなく左右どちらかに寄せる（3値に落とすとき
# 中道に吸わせると center が肥大化し、左右の並置比較が痩せるため）。
# "liberal" "conservative" "neutral" のように国や文脈で意味が変わる語はあえて載せない
# （黙って誤分類するより ValueError で止めて人に決めさせる）。
LEAN_VALUE_ALIASES = {
    "right": "right", "right_wing": "right", "center_right": "right",
    "centre_right": "right",
    "center": "center", "centre": "center", "centrist": "center",
    "left": "left", "left_wing": "left", "center_left": "left",
    "centre_left": "left",
    "na": "na", "n/a": "na", "none": "na", "unknown": "na",
}
GOV_AXIS_VALUE_ALIASES = {
    "pro_gov": "pro_gov", "pro_government": "pro_gov", "progov": "pro_gov",
    "progovernment": "pro_gov",
    "indep": "indep", "independent": "indep", "independiente": "indep",
    "independant": "indep",
    "anti_gov": "anti_gov", "anti_government": "anti_gov", "antigov": "anti_gov",
    "antigovernment": "anti_gov", "opposition": "anti_gov",
    "na": "na", "n/a": "na", "none": "na", "unknown": "na",
}
CONFIDENCE_VALUE_ALIASES = {
    "high": "high", "medium": "medium", "med": "medium", "mid": "medium",
    "moderate": "medium", "low": "low",
}
ROLE_VALUE_ALIASES = {
    "primary": "primary", "main": "primary", "backup": "backup",
    "secondary": "backup", "fallback": "backup", "reserve": "backup",
}
_VALUE_ALIASES = {
    "lean": LEAN_VALUE_ALIASES,
    "gov_axis": GOV_AXIS_VALUE_ALIASES,
    "confidence": CONFIDENCE_VALUE_ALIASES,
    "role": ROLE_VALUE_ALIASES,
}


# ==================================================================
# feeds.json の読み込みと検証（契約§1）
# ==================================================================

def _label_token(value):
    """'Pro-Government' -> 'pro_government'。値の別名照合用。"""
    return "_".join(str(value).strip().lower().replace("-", " ").split())


def normalize_feed_labels(feed):
    """feeds.json の1エントリの論調ラベルを正規形に寄せた**新しい dict**を返す。

    - キーの別名（FEED_KEY_ALIASES）を正規キーへ改名する。
      正規キーと別名が両方あって値が食い違う場合は ValueError（どちらが正か決められない）。
    - lean / gov_axis / confidence / role の値の別名を正規値へ寄せる。
      辞書に無い値はそのまま残す（判定は validate_feed_labels が行う）。
    - sources が文字列1本なら配列に包む。
    - フィールド欠落はそのまま（既定値を書き足さない。記事側で "na" を補う）。
    """
    out = dict(feed)

    # 初期の分類投入では basis を「論調・政権軸・根拠URL」の入れ子で
    # 保存したフィードがある。正規形では各根拠を別キーにするため、
    # alias 処理の前に展開する（sources は後段で配列を検証する）。
    basis = out.pop("basis", None)
    if isinstance(basis, dict):
        for source_key, target_key in (("lean", "lean_basis"),
                                       ("gov_stance", "gov_axis_basis"),
                                       ("gov_axis", "gov_axis_basis"),
                                       ("sources", "sources")):
            if source_key not in basis:
                continue
            value = basis[source_key]
            if target_key in out and out[target_key] != value:
                raise ValueError("feeds.json: %s の %s と basis.%s が食い違っています"
                                 % (feed.get("feed_id"), target_key, source_key))
            out[target_key] = value
    elif basis is not None:
        # basis が文字列だった旧形式は論調根拠として扱う。
        if "lean_basis" in out and out["lean_basis"] != basis:
            raise ValueError("feeds.json: %s の lean_basis と basis が食い違っています"
                             % feed.get("feed_id"))
        out.setdefault("lean_basis", basis)

    for alias, canon in FEED_KEY_ALIASES.items():
        if alias not in out:
            continue
        value = out.pop(alias)
        if canon in out and out[canon] != value:
            raise ValueError("feeds.json: %s の %s と %s が食い違っています（%r / %r）"
                             % (feed.get("feed_id"), canon, alias, out[canon], value))
        out[canon] = value
    for key, table in _VALUE_ALIASES.items():
        if isinstance(out.get(key), str):
            token = _label_token(out[key])
            out[key] = table.get(token, table.get(token.replace("_", ""), out[key]))
    if isinstance(out.get("sources"), str):
        out["sources"] = [out["sources"]]
    # 根拠は表示用の短い説明として文字列に統一する。配列を許容する
    # ことで、複数根拠を持つ追加フィードも読み込み時に落とさない。
    for key in ("lean_basis", "gov_axis_basis"):
        if isinstance(out.get(key), list):
            out[key] = "；".join(str(value) for value in out[key])
    return out


def validate_feed_labels(feed):
    """正規化済みエントリの任意フィールドを契約§1の列挙で検証する。列挙外は ValueError。"""
    fid = feed.get("feed_id")
    for key, allowed in (("lean", VALID_LEANS), ("gov_axis", VALID_GOV_AXES),
                         ("confidence", VALID_CONFIDENCES), ("role", VALID_ROLES)):
        if key in feed and feed[key] not in allowed:
            raise ValueError("feeds.json: %s の %s %r は契約§1の列挙外です"
                             % (fid, key, feed[key]))
    for key in ("lean_basis", "gov_axis_basis"):
        if key in feed and not isinstance(feed[key], str):
            raise ValueError("feeds.json: %s の %s は文字列にしてください" % (fid, key))
    if "classified_at" in feed:
        try:
            datetime.strptime(str(feed["classified_at"]), "%Y-%m-%d")
        except ValueError:
            raise ValueError("feeds.json: %s の classified_at %r は YYYY-MM-DD ではありません"
                             % (fid, feed["classified_at"]))
    if "sources" in feed:
        srcs = feed["sources"]
        if not isinstance(srcs, list) or not all(
                isinstance(u, str) and u.startswith(("http://", "https://")) for u in srcs):
            raise ValueError("feeds.json: %s の sources はURL配列にしてください" % fid)


def load_feeds(path=None):
    """feeds.json を読んで「有効なフィード定義のリスト」を返す。

    enabled:false のフィードは**返さない**が、死亡確認の記録として
    feeds.json には残しておく（消すと「調べた履歴」が失われ、
    次の人が同じ死んだURLを再登録してしまう）。
    """
    path = path or FEEDS_PATH
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    feeds = doc.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        raise ValueError("%s に feeds 配列がありません" % path)

    # 別名を正規形へ寄せてから検証する（検証は正規形に対してだけ書けばよい）。
    feeds = [normalize_feed_labels(f) for f in feeds]
    seen_ids = set()
    for feed in feeds:
        validate_feed_labels(feed)
        for key in ("feed_id", "source", "country", "media_type", "lang", "rss_url"):
            if not feed.get(key):
                raise ValueError("feeds.json: %r に必須キー %s がありません"
                                 % (feed.get("feed_id") or feed, key))
        if feed["media_type"] not in VALID_MEDIA_TYPES:
            raise ValueError("feeds.json: %s の media_type %r は契約§1の列挙外です"
                             % (feed["feed_id"], feed["media_type"]))
        if feed["feed_id"] in seen_ids:
            raise ValueError("feeds.json: feed_id %r が重複しています" % feed["feed_id"])
        seen_ids.add(feed["feed_id"])
        feed.setdefault("stale_after_days", DEFAULT_STALE_AFTER_DAYS)

    return [f for f in feeds if f.get("enabled")]


def host_of(url):
    """URLからホスト名を取り出す。レート制御のキーに使う。"""
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def interleave_by_host(feeds):
    """同一ホストが連続しないように取得順を並べ替える（本タスク要件9）。

    26本の中には bbci.co.uk（BBC 2本）や feeds.bbci.co.uk のように
    同じホストを複数持つものがある。定義順のまま叩くと同一サーバに
    連続で当たって迷惑になりやすい。ホストごとの列を作り、各列から
    1本ずつ順に取り出す（ラウンドロビン）ことで間隔を自然に空ける。

    元の順序は各ホストの列の中では保つので、結果は決定的（テストしやすい）。
    """
    buckets = {}
    order = []
    for feed in feeds:
        host = host_of(feed["rss_url"])
        if host not in buckets:
            buckets[host] = []
            order.append(host)
        buckets[host].append(feed)

    result = []
    while any(buckets[h] for h in order):
        for host in order:
            if buckets[host]:
                result.append(buckets[host].pop(0))
    return result


# ==================================================================
# 時刻ユーティリティ（フェーズ0から引き継ぎ）
# ==================================================================

def utc_now_iso():
    """現在時刻をUTCのISO8601（秒まで・末尾Z）で返す。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def jst_today():
    """日別ファイルの日付キー（JST基準）。

    記事1件ごとの published_at は UTC に正規化して保存するが、
    「何月何日のページか」だけはJSTで決める。サイトの表示をJSTに統一しており、
    JSTの朝に動かす前提なのでユーザーの感覚と一致する。
    """
    return datetime.now(JST).strftime("%Y-%m-%d")


def jst_date_key(generated_at_utc):
    """UTCのISO8601文字列 -> JSTの暦日キー 'YYYY-MM-DD'。

    jst_today() は「今日」を見るので、保存済みの generated_at から
    後から同じ日付キーを再現することができない（再生成が再現しない）。
    日別ドキュメントの date は常に generated_at から導く。
    """
    dt = parse_utc_iso(generated_at_utc)
    if dt is None:
        # generated_at が壊れているのは想定外。黙って違う日付を書くより
        # 実行時のJST今日にフォールバックする（形式だけは必ず満たす）。
        return jst_today()
    return dt.astimezone(JST).strftime("%Y-%m-%d")


def sort_articles_newest_first(articles):
    """記事を公開時刻の降順に並べる。published_at が空のものは末尾。

    merge_records の並び順と同じ規則にする（日付不明は元の順番を保つ）。
    日別JSONと追記後のJSONで並び順が違うと git 差分が無意味に膨らむ。
    """
    ordered = list(articles)
    index = {id(a): i for i, a in enumerate(ordered)}

    def sort_key(article):
        dt = parse_utc_iso(article.get("published_at"))
        if dt:
            return (0, -dt.timestamp(), 0)
        return (1, 0, index[id(article)])

    return sorted(ordered, key=sort_key)


def month_of(date_key):
    """'2026-09-15' -> '2026-09'。月別シャードのファイル名に使う。"""
    return date_key[:7]


def month_path(month):
    return os.path.join(MONTHS_DIR, "%s.json" % month)


def day_path_for(date_key):
    """'2026-09-15' -> data/days/2026-09-15.json。"""
    return os.path.join(DAYS_DIR, "%s.json" % date_key)


def to_utc_iso(date_text):
    """フィードの日付文字列をUTCのISO8601に揃える。読めなければ None。

    RSS2.0 の <pubDate> は RFC822（Mon, 14 Sep 2026 07:05:00 +0900）だが、
    Atom の <updated> と RSS1.0/RDF の <dc:date> は ISO8601。
    両方扱わないと GOV.UK（Atom）や朝日・DW（RDF）の日付が全部 None になり、
    「件数はあるのに鮮度が判定できない」状態になって鮮度監視が成立しない。
    """
    if not date_text:
        return None
    raw = str(date_text).strip()
    if not raw:
        return None

    dt = None
    # ISO8601系を先に試す。'2026-09-15' のような日付のみも fromisoformat が読む。
    if raw[:1].isdigit():
        iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            dt = None
    if dt is None:
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            dt = None
    if dt is None:
        return None

    if dt.tzinfo is None:                  # タイムゾーン未記載はJSTとみなす
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_iso(text):
    """保存済みのUTC ISO8601文字列を datetime に戻す。読めなければ None。"""
    if not text:
        return None
    raw = str(text).strip()
    if raw.endswith("Z"):                  # 3.11未満の fromisoformat は 'Z' を読めない
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ==================================================================
# ダウンロード（レート配慮つき）
# ==================================================================

class HostThrottle:
    """同一ホストへのアクセス間隔を空ける小さなゲート（本タスク要件9）。

    取得順のインターリーブで同一ホスト連続は基本的に避けているが、
    ホストが1つしかない場合や --only で絞った場合は連続しうる。
    最後に叩いた時刻を覚えておき、間隔が足りなければその分だけ寝る。
    """

    def __init__(self, min_interval=PER_HOST_MIN_INTERVAL_SEC, sleep=time.sleep,
                 clock=time.monotonic):
        self.min_interval = min_interval
        self._last = {}
        self._sleep = sleep      # テストから差し替えて実際には寝かせないため
        self._clock = clock

    def wait(self, url):
        """必要なら待つ。実際に待った秒数を返す（ログ・テスト用）。"""
        host = host_of(url)
        now = self._clock()
        last = self._last.get(host)
        waited = 0.0
        if last is not None:
            remain = self.min_interval - (now - last)
            if remain > 0:
                self._sleep(remain)
                waited = remain
                now = self._clock()
        self._last[host] = now
        return waited


def download(url, throttle=None, timeout=TIMEOUT_SEC, max_retry=MAX_RETRY, log=print):
    """RSSを取得して bytes を返す。max_retry 回試して全部だめなら最後の例外を投げる。

    呼び出し側（collect_feed）が例外を捕まえて failed にするので、
    ここでは「諦めたら投げる」だけにしてある（A8）。
    """
    if throttle is not None:
        waited = throttle.wait(url)
        if waited > 0:
            log("    同一ホスト連続を避けるため %.1f秒待機" % waited)

    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "ja,en;q=0.8",
        # 圧縮を要求しない。gzip を自分で解くと標準ライブラリだけでも書けるが、
        # フィード1本あたり数十KBなので伸ばす価値がなく、分岐を増やさない方が壊れにくい。
        "Accept-Encoding": "identity",
    })
    last_error = None
    for attempt in range(1, max_retry + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                if res.status != 200:
                    raise urllib.error.HTTPError(
                        url, res.status, "unexpected status", res.headers, None)
                body = res.read()
            log("    取得成功 (%d回目, %d bytes)" % (attempt, len(body)))
            return body
        except Exception as e:      # ネットワーク系は飛んでくる型の幅が広いので広く捕る
            last_error = e
            log("    取得失敗 (%d/%d回目): %s: %s" % (attempt, max_retry, type(e).__name__, e))
            if attempt < max_retry:
                wait = RETRY_WAIT_SEC * attempt
                log("    %d秒待って再試行します" % wait)
                time.sleep(wait)
    raise last_error


# ==================================================================
# RSS/Atom パーサ
#
# ★A7（回帰防止の対象）
#   ElementTree のタグ名は名前空間付きだと "{http://purl.org/rss/1.0/}item" になる。
#   そのため findall(".//item") は名前空間付きフィードに **1件もマッチしない**。
#   RSS 1.0(RDF) の朝日・DW、Atom の GOV.UK が「HTTP 200 なのに0件」だった原因。
#   ここでは必ず local_name() でローカル名だけを見て比較する。
#   → ".//item" 直指定に戻すと scripts/test_fetch.py が落ちる。
# ==================================================================

def local_name(tag):
    """'{http://purl.org/rss/1.0/}item' -> 'item'。名前空間を取り除く。"""
    if not isinstance(tag, str):
        return ""      # コメントノードなどは tag が関数オブジェクトになる
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def find_entries(root):
    """フィード形式を問わず「記事1件」に相当する要素を出現順で返す。

      RSS 2.0 : <rss><channel><item>
      RSS 1.0 : <rdf:RDF><item>      （channelの外に並ぶ。名前空間あり）
      Atom    : <feed><entry>        （名前空間あり）
    """
    return [el for el in root.iter() if local_name(el.tag) in ("item", "entry")]


def child_text(parent, name):
    """直下の子要素のテキストを名前空間に関係なく取る。無ければ None。"""
    for child in parent:
        if local_name(child.tag) == name:
            text = (child.text or "").strip()
            if text:
                return text
    return None


def extract_link(item):
    """記事URLを取り出す。

    RSS は <link>本文がURL</link>、Atom は <link href="..."/> と形が違う。
    Atom には rel="self" / rel="replies" も並ぶので rel="alternate"（既定）を選ぶ。
    ここを素朴に「最初の<link>」にすると GOV.UK でフィード自身のURLを
    記事URLとして拾ってしまい、全記事が同じ article_id になる。
    """
    alternate = None
    fallback = None
    for child in item:
        if local_name(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if href:
            rel = (child.get("rel") or "alternate").strip()
            if rel == "alternate" and alternate is None:
                alternate = href
            elif fallback is None:
                fallback = href
            continue
        text = (child.text or "").strip()      # RSS形式の <link>URL</link>
        if text and fallback is None:
            fallback = text
    url = alternate or fallback
    if not url:
        # 最後の砦：RSS 1.0 では <guid>/<id> にURLが入っていることがある
        for name in ("guid", "id"):
            cand = child_text(item, name)
            if cand and cand.startswith("http"):
                return cand
        return None
    return url


def extract_published(item):
    """公開時刻をUTC ISO8601で返す。フィード形式ごとにタグ名が違うので順に試す。

      RSS 2.0 : <pubDate>Mon, 14 Sep 2026 07:00:00 +0900</pubDate>  （RFC822）
      RSS 1.0 : <dc:date>2026-09-14T07:00:00+09:00</dc:date>         （ISO8601）
      Atom    : <published> / <updated>                              （ISO8601）
    """
    raw = child_text(item, "pubDate")
    if raw:
        got = to_utc_iso(raw)
        if got:
            return got
    for name in ("date", "published", "updated", "modified"):
        raw = child_text(item, name)
        if not raw:
            continue
        dt = parse_utc_iso(raw)
        if dt:
            return dt.astimezone(timezone.utc).replace(
                microsecond=0).isoformat().replace("+00:00", "Z")
        got = to_utc_iso(raw)      # 稀に dc:date に RFC822 が入るフィードがある
        if got:
            return got
    return None


def make_article_id(url):
    """article_id = sha256(url) の先頭8桁（契約§2）。

    URLが同じなら必ず同じIDになるので、同日2回実行しても記事が増えない（A2）。
    8桁（32bit）は26本×40件＝最大1040件/日の規模では衝突がほぼ起きない
    （誕生日問題で約1/4000）。衝突しても後段はURLで一意にマージするので
    記事が消えることはなく、IDが重複するだけ。
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]


def parse_items(xml_bytes, feed, fetched_at, max_items=MAX_ITEMS_PER_FEED):
    """RSS/Atom のXMLから記事レコードの一覧を作る。

    RSS 2.0 / RSS 1.0 (RDF) / Atom の3形式に対応する（A7）。
    <description> / <content:encoded> / <summary> は **読み取り自体をしない**。
    出力は ALLOWED_ARTICLE_FIELDS のキーのみ（C1・A1）。
    """
    root = ET.fromstring(xml_bytes)     # 壊れたXMLなら ET.ParseError（= 失敗扱い）
    records = []
    rank = 0
    for item in find_entries(root):
        title = child_text(item, "title")
        link = extract_link(item)
        if not title or not link:       # 見出しかリンクが無いものは使えないので捨てる
            continue
        rank += 1                       # rank は「採用できた記事」の中での順位（1始まり）
        record = {
            "article_id": make_article_id(link),
            "feed_id": feed["feed_id"],
            "source": feed["source"],
            "country": feed["country"],
            "media_type": feed["media_type"],
            "lang": feed["lang"],
            "title_original": title,
            "url": link,
            "published_at": extract_published(item),
            "fetched_at": fetched_at,
            "rank_in_feed": rank,
            # 論調はフィード単位の分類。未分類フィードも同じキー集合にするため "na" で埋める
            "lean": feed.get("lean") or LABEL_NA,
            "gov_axis": feed.get("gov_axis") or LABEL_NA,
        }
        # ホワイトリストで絞り直す。上で作った dict にキーを足す改造が入っても
        # ここで落ちるので、本文キーが出力に混ざらない（C1）。
        records.append({k: record[k] for k in ALLOWED_ARTICLE_FIELDS})
        if max_items and rank >= max_items:
            break
    return records


# ==================================================================
# 鮮度（最新記事の日付）による死活監視 ── 本タスク要件7
#
#   件数を数える監視では検出できない失敗が実在する（実測で確認済み）。
#     VOA World  : HTTP 200 で20件返すが最新記事が 2025-03-15（1年半前で凍結）
#     人民網     : HTTP 200 で100件返すが最新記事が 2025-06-05（1年3か月前で凍結）
#     JPost(旧URL): HTTP 200 で10件返すが最新記事が 2025-06-16（1年3か月前で凍結）
#   どれも「件数 > 0」なので件数監視では永久に検出できない。
#   そこで「何件返ってきたか」ではなく「最新記事がいつか」を必ず記録する。
# ==================================================================

def latest_published(records):
    """記事一覧の中で最も新しい published_at を返す。1件も日付が読めなければ None。"""
    stamps = [parse_utc_iso(r.get("published_at")) for r in records]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def check_freshness(records, feed, now=None):
    """フィード1本の取得状況を判定して dict で返す（契約§2 feeds[] の形）。"""
    now = now or datetime.now(timezone.utc)
    threshold = int(feed.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS))
    result = {
        "feed_id": feed["feed_id"],
        "source": feed["source"],
        "country": feed["country"],
        "rss_url": feed["rss_url"],
        "articles": len(records),
        "latest_published_at": None,
        "stale_days": None,
        "threshold_days": threshold,
        "status": STATUS_OK,
        "error": None,
        "note": "",
    }

    # HTTP 200 でも0件なら failed と区別して empty にする。
    # 「接続はできている＝ネットワークの問題ではない」ことが分かる方が原因を追いやすい。
    if not records:
        result["status"] = STATUS_EMPTY
        result["note"] = "接続はできましたが記事が0件でした（配信形式の変更が疑われます）"
        return result

    latest = latest_published(records)
    if latest is None:
        # 件数はあるが日付が1つも読めない。鮮度を判定できないので stale 扱いにして
        # 「分からない」を「正常」に丸めない。
        result["status"] = STATUS_STALE
        result["note"] = "記事の日付を読み取れず、鮮度を確認できませんでした"
        return result

    result["latest_published_at"] = latest.replace(
        microsecond=0).isoformat().replace("+00:00", "Z")
    age_days = (now - latest).total_seconds() / 86400.0
    result["stale_days"] = round(max(age_days, 0.0), 1)

    if age_days > threshold:
        result["status"] = STATUS_STALE
        result["note"] = ("最新記事が%.1f日前で、閾値%d日を超えています"
                          "（件数は%d件あるため接続自体は成功しています）"
                          % (result["stale_days"], threshold, len(records)))
    else:
        result["note"] = "最新記事は%.1f日前です" % result["stale_days"]
    return result


def failed_status(feed, error):
    """取得失敗を日次メタデータへ残す。次回成功時は通常のok状態へ戻る。"""
    return {
        "feed_id": feed["feed_id"],
        "source": feed["source"],
        "country": feed["country"],
        "rss_url": feed["rss_url"],
        "articles": 0,
        "latest_published_at": None,
        "stale_days": None,
        "threshold_days": int(feed.get("stale_after_days", DEFAULT_STALE_AFTER_DAYS)),
        "status": STATUS_FAILED,
        "error": str(error),
        "note": "取得失敗・今回の取得対象外: %s" % error,
    }


def worst_status(feed_statuses):
    """フィード群の状態からその日の総合状態を決める。

    ★フェーズ0からの変更点
      フェーズ0はフィードが1本しかなかったので「最も悪いもの」を採用していた。
      26本になると1本でも死ねば必ず failed になり、総合状態が常に failed に
      張り付いて意味を失う（実測でも 26本中3本が stale/failed）。
      そこで「記事が取れたフィードが1本でもあれば全体は前進している」と捉え、
      - 1本でも ok があれば ok
      - ok は無いが記事は取れている（stale）なら stale
      - 記事が1件も取れていないなら empty / failed
      とする。個別の異常は feeds[] と alerts に必ず残るので情報は失われない。
    """
    found = [s.get("status") for s in feed_statuses]
    if not found:
        return STATUS_FAILED
    if STATUS_OK in found:
        return STATUS_OK
    if STATUS_STALE in found:
        return STATUS_STALE
    if STATUS_EMPTY in found:
        return STATUS_EMPTY
    return STATUS_FAILED


# ==================================================================
# 月別シャード構成の読み書き（フェーズ0から引き継ぎ）
#
#   data/index.json は「月の一覧＋最終更新」だけを持つ軽量マニフェスト。
#   日数が増えても中身が増えないので、毎日コミットしても履歴が膨らまない。
#   日別メタは data/months/YYYY-MM.json に入れ、更新はその月のファイルに閉じる。
# ==================================================================

def load_json(path, default):
    """JSONを読む。無い・壊れている場合は default を返す。"""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        print("  警告: %s を読めないので初期状態から作り直します (%s)" % (path, e))
        return default


def save_json(path, payload):
    """JSONを書く。UTF-8・日本語そのまま・末尾に改行（gitの差分が読みやすい）。

    中身が前回と同じなら generated_at を据え置き、1バイトも変えない（A2）。
    日次実行で記事が1件も増えなかった場合に無意味なコミットを生ませないため。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(payload, dict) and "generated_at" in payload and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and "generated_at" in old:
                probe = dict(payload)
                probe["generated_at"] = old["generated_at"]
                if probe == old:
                    payload = probe
        except (ValueError, OSError):
            pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")


def update_month_shard(date_key, day_summary):
    """その月のシャードだけを更新する。他の月のファイルには一切触らない。"""
    month = month_of(date_key)
    path = month_path(month)
    shard = load_json(path, {})
    if not isinstance(shard, dict) or "days" not in shard:
        shard = {"schema_version": SCHEMA_VERSION, "month": month, "days": {}}
    shard["schema_version"] = SCHEMA_VERSION
    shard["month"] = month
    # 同じ日付キーへの上書き。これで同日2回実行でも日数が増えない（A2）。
    shard["days"][date_key] = day_summary
    shard["days"] = {k: shard["days"][k] for k in sorted(shard["days"])}
    shard["updated_at"] = utc_now_iso()
    save_json(path, shard)
    return path, len(shard["days"])


def update_index(date_key):
    """軽量マニフェストを更新する。中身は「月の一覧」「最終更新」「最新日」だけ。

    日別のメタは一切入れない。ここに日別メタを足すと旧構造に戻ってしまい、
    1年で .git が1GBに達する。
    """
    month = month_of(date_key)
    index = load_json(INDEX_PATH, {})
    months = index.get("months") if isinstance(index, dict) else None
    if not isinstance(months, list):
        months = []
    if isinstance(index, dict) and isinstance(index.get("days"), dict):
        months.extend(month_of(k) for k in index["days"].keys())   # 旧構造からの移行
    if month not in months:
        months.append(month)
    months = sorted(set(m for m in months if isinstance(m, str) and len(m) == 7))

    payload = {
        "schema_version": SCHEMA_VERSION,
        "months": months,
        "latest_date": max([date_key] + [
            d for d in [index.get("latest_date")] if isinstance(d, str)
        ]),
        "updated_at": utc_now_iso(),
    }
    save_json(INDEX_PATH, payload)
    return payload


# ==================================================================
# 冪等な保存（同じ日に何回動かしても二重登録しない）── A2
# ==================================================================

def merge_records(existing, incoming):
    """既存の記事一覧に新しい取得結果を混ぜる。URLが同じものは1件に寄せる。

    同日2回目の実行では1回目と同じ記事が返ってくる。URLで重複排除することで
    件数が2倍にならない（A2）。article_id は sha256(url) なので
    「URLで一意」＝「article_id で一意」になり、②③④の参照も壊れない。
    """
    merged = {}
    order = []
    for record in list(existing) + list(incoming):
        url = record.get("url")
        if not url:
            continue
        if url not in merged:
            order.append(url)
        else:
            # 2回目に出てきた同一URL。fetched_at は最初の値を残して
            # 「いつ初めて見つけたか」を保つ。rank_in_feed も初回の値を尊重する
            # （フィード内順位は時間とともに下がるが、初出時の扱いの方が情報量がある）。
            record = dict(record)
            record["fetched_at"] = merged[url].get("fetched_at", record.get("fetched_at"))
            if merged[url].get("rank_in_feed") is not None:
                record["rank_in_feed"] = merged[url]["rank_in_feed"]
        merged[url] = {k: record.get(k) for k in ALLOWED_ARTICLE_FIELDS}
        # lean/gov_axis 導入前に保存された記事にはキーが無い。None ではなく "na" で埋める
        for label in ("lean", "gov_axis"):
            if not merged[url][label]:
                merged[url][label] = LABEL_NA

    # 新しい記事が上に来るように公開時刻の降順。日付不明は末尾へ。
    def sort_key(url):
        dt = parse_utc_iso(merged[url].get("published_at"))
        return (0, -dt.timestamp(), 0) if dt else (1, 0, order.index(url))

    return [merged[u] for u in sorted(order, key=sort_key)]


def is_forbidden_key(key):
    """キー名が本文系かどうかを判定する（C1・A1）。

    完全一致だけだと "articleBody" や "contentEncoded" のような表記ゆれを
    見逃すので、英数字だけに正規化して部分一致でも見る。
    """
    if not isinstance(key, str):
        return False
    norm = "".join(ch for ch in key.lower() if ch.isalnum())
    if norm in FORBIDDEN_ALLOWLIST:
        return False
    if norm in _FORBIDDEN_NORMALIZED:
        return True
    return any(sub in norm for sub in FORBIDDEN_SUBSTRINGS)


def assert_no_body_fields(payload, where):
    """本文系のキーがJSONに1つも無いことを再帰的に確認する（C1・A1）。

    ホワイトリスト方式なので理屈の上では入り得ないが、将来の改造で
    ALLOWED_ARTICLE_FIELDS に本文キーを足してしまう事故を実行時に止める最後の砦。
    """
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if is_forbidden_key(key):
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


# ==================================================================
# 収集本体 ── 1本ずつ独立に取り、失敗しても次へ進む（A8）
# ==================================================================

def collect_feed(feed, fetched_at, throttle=None, now=None, offline_dir=None, log=print):
    """フィード1本を取得して (記事リスト, 状態レコード) を返す。**例外を外に出さない**。

    A8（1本が失敗・タイムアウトしても残りの収集が続く）をここで担保する。
    ネットワーク例外・XMLパースエラー・想定外の例外まで全部ここで受け止め、
    `status:"failed"` の状態レコードに変換して返す。呼び出し側はループを続けられる。
    """
    try:
        if offline_dir:
            # オフライン確認用。<feed_id>.xml を読む。ネットには一切出ない。
            path = os.path.join(offline_dir, "%s.xml" % feed["feed_id"])
            with open(path, "rb") as f:
                xml_bytes = f.read()
            log("    オフライン読み込み: %s (%d bytes)" % (path, len(xml_bytes)))
        else:
            xml_bytes = download(feed["rss_url"], throttle=throttle, log=log)
    except Exception as e:
        # ここで握るのが A8 の本質。1本の死が全体を止めない。
        error = "%s: %s" % (type(e).__name__, e)
        log("    → failed（この1本を諦めて次のフィードへ進みます）")
        return [], failed_status(feed, error)

    try:
        records = parse_items(xml_bytes, feed, fetched_at)
    except ET.ParseError as e:
        # HTTP 200 でも中身がXMLでないことがある（HTMLのエラーページ・同意ページ等）。
        log("    → failed（XMLとして読めません）")
        return [], failed_status(feed, "XMLとして読めませんでした: %s" % e)
    except Exception as e:
        log("    → failed（想定外のパースエラー）")
        return [], failed_status(feed, "解析に失敗しました: %s: %s" % (type(e).__name__, e))

    status = check_freshness(records, feed, now=now)
    log("    → %s / %d件 / 最新記事 %s"
        % (status["status"], status["articles"], status["latest_published_at"] or "不明"))
    return records, status


def collect_all(feeds, fetched_at, throttle=None, now=None, offline_dir=None,
                inter_feed_wait=INTER_FEED_WAIT_SEC, sleep=time.sleep, log=print):
    """全フィードを順次取得して (記事リスト, 状態レコードリスト) を返す。

    取得順は interleave_by_host() で同一ホストが連続しないよう並べ替える（要件9）。
    状態レコードは **feeds.json の定義順** に並べ直して返す。
    取得順（ホスト分散のため入り乱れる）で出力すると、日別JSONの feeds[] の並びが
    実行ごとにブレて git 差分が読みにくくなるため。
    """
    order = interleave_by_host(feeds)
    all_records = []
    statuses = {}

    for i, feed in enumerate(order, 1):
        log("  [%d/%d] %s (%s / %s)"
            % (i, len(order), feed["source"], feed["country"], feed["feed_id"]))
        records, status = collect_feed(
            feed, fetched_at, throttle=throttle, now=now, offline_dir=offline_dir, log=log)
        all_records.extend(records)
        statuses[feed["feed_id"]] = status
        # 最後の1本の後には待たない（無駄に実行時間を伸ばさない）
        if inter_feed_wait and i < len(order):
            sleep(inter_feed_wait)

    ordered_statuses = [statuses[f["feed_id"]] for f in feeds if f["feed_id"] in statuses]
    return all_records, ordered_statuses


def build_day_summary(articles, feed_statuses, generated_at):
    """契約§2 の days/<D>.json 本体（1日分の完成品）を組み立てて返す。

    以前この関数は月別シャード用の圧縮メタ（status/articles件数/alerts…）を
    返していた。しかし契約§2 と test_fetch.py が求めているのは
    `schema_version / date / generated_at / status / feeds / articles` を持つ
    **日別ドキュメントそのもの**。呼び名が同じまま別物を返していたため
    TestDaySummary の5件が KeyError で落ちていた。
    月別シャード用の圧縮メタは build_day_meta() に分離した。

    date は契約§2 の通り JST の暦日。generated_at（UTC）から換算する。
    articles は公開時刻の降順（新しい順・日付不明は末尾）に整える。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "date": jst_date_key(generated_at),
        "generated_at": generated_at,
        "status": worst_status(feed_statuses),
        # 取得状況は必ず日別JSONに残す（欠損を隠さない方針）。
        "feeds": list(feed_statuses),
        # 記事は「見出し＋原文リンク」のホワイトリスト列だけ。本文は持たない（A1）。
        "articles": sort_articles_newest_first(articles),
    }


def build_day_meta(day_document):
    """月別シャードに入れる、その日1行分の圧縮メタ情報。

    件数だけでなく「異常があったフィード」を必ず持たせる。
    index.html はこれを見て「⚠ 鮮度異常」を表示する（欠損を隠さない方針）。
    """
    articles = day_document["articles"]
    feed_statuses = day_document["feeds"]
    return {
        "status": day_document["status"],
        "articles": len(articles),
        # 12カ国そろっているかがカレンダー上で分かるように国数も持たせる。
        "countries": len({a.get("country") for a in articles if a.get("country")}),
        "feeds_ok": sum(1 for s in feed_statuses if s["status"] == STATUS_OK),
        "feeds_total": len(feed_statuses),
        "generated_at": day_document["generated_at"],
        # 異常なフィードだけを残す。正常な日はここが空配列になるので
        # 月別シャードが日数分だけ素直に増える（1日あたり数百バイト）。
        "alerts": [
            {
                "feed_id": s["feed_id"],
                "source": s["source"],
                "country": s.get("country"),
                "status": s["status"],
                "error": s.get("error"),
                "latest_published_at": s["latest_published_at"],
                "stale_days": s["stale_days"],
                "threshold_days": s["threshold_days"],
                "note": s["note"],
            }
            for s in feed_statuses if s["status"] != STATUS_OK
        ],
    }


def run(feeds, date_key, throttle=None, now=None, offline_dir=None, dry_run=False,
        inter_feed_wait=INTER_FEED_WAIT_SEC, sleep=time.sleep, log=print):
    """収集から保存までを1回分実行して (day_payload, day_summary) を返す。

    ネットワーク処理は collect_feed に閉じているので、テストからは
    offline_dir を渡してローカルの固定XMLだけで通しで呼べる。
    """
    generated_at = utc_now_iso()
    articles, feed_statuses = collect_all(
        feeds, generated_at, throttle=throttle, now=now, offline_dir=offline_dir,
        inter_feed_wait=inter_feed_wait, sleep=sleep, log=log)

    status = worst_status(feed_statuses)
    day_path = day_path_for(date_key)

    if not articles:
        # 1件も取れなかった。空の日別ファイルを置いて「0件の日」と誤読させたくないので
        # 既存の成功済みファイルがあればそれを壊さず残す（冪等）。
        existing = load_json(day_path, None)
        if isinstance(existing, dict) and existing.get("articles"):
            log("  今回0件ですが既存の %s.json は成功済みなので上書きしません" % date_key)
            # 既存ファイルをそのまま日別ドキュメントとして返し、
            # 月別シャード用メタはその内容から作る。
            return existing, build_day_meta({
                "status": existing.get("status", worst_status(feed_statuses)),
                "generated_at": existing.get("generated_at", generated_at),
                "feeds": existing.get("feeds", feed_statuses),
                "articles": existing["articles"],
            })
        meta = build_day_meta(build_day_summary([], feed_statuses, generated_at))
        if not dry_run:
            shard_path, day_count = update_month_shard(date_key, meta)
            update_index(date_key)
            log("  取得失敗を %s に記録しました（%d日分）" % (shard_path, day_count))
        return None, meta

    # --- 冪等マージ：同日に既存ファイルがあればURLで重複排除して混ぜる（A2）---
    existing = load_json(day_path, None)
    existing_articles = existing.get("articles", []) if isinstance(existing, dict) else []
    if existing_articles:
        log("  既存の %s.json に %d件あります。URLで重複排除して統合します"
            % (date_key, len(existing_articles)))
    articles = merge_records(existing_articles, articles)

    # 日別ドキュメントの組み立ては build_day_summary に一本化する。
    # （以前はここで dict を手組みしていたため、テストが見ている
    #   build_day_summary の戻りと実際に保存される形が二重管理になっていた）
    day_payload = build_day_summary(articles, feed_statuses, generated_at)
    # date は generated_at 由来だが、保存先ファイル名（date_key）と必ず一致させる。
    day_payload["date"] = date_key

    # 保存する直前に本文系キーが無いことを確認する。あればここで止まる（C1・A1）。
    assert_no_body_fields(day_payload, "day")
    meta = build_day_meta(day_payload)
    assert_no_body_fields(meta, "summary")

    if dry_run:
        log("  --dry-run なので保存しません（%d件）" % len(articles))
        return day_payload, meta

    save_json(day_path, day_payload)
    shard_path, day_count = update_month_shard(date_key, meta)
    index = update_index(date_key)
    log("  保存: %s (%d件)" % (day_path, len(articles)))
    log("  更新: %s (この月 %d日分)" % (shard_path, day_count))
    log("  更新: %s (月一覧 %s)" % (INDEX_PATH, ",".join(index["months"])))
    return day_payload, meta


# ==================================================================
# メイン
# ==================================================================

def print_country_coverage(feeds, feed_statuses, log=print):
    """12カ国×2媒体の充足状況を実測値で表示する（A10の実行時確認）。

    feeds.json の enabled 本数だけでは「設定上はそろっている」ことしか分からない。
    実際に記事が取れた本数まで出して、当日の実力を毎回ログに残す。
    """
    by_country = {}
    for feed in feeds:
        by_country.setdefault(feed["country"], []).append(feed["feed_id"])
    got = {s["feed_id"]: s for s in feed_statuses}

    log("-" * 72)
    log("12カ国×2媒体の充足（実測）")
    log("  国  有効  記事取得できた本数  判定")
    shortfall = []
    for country in sorted(by_country):
        ids = by_country[country]
        alive = [i for i in ids if got.get(i, {}).get("articles", 0) > 0]
        ok = len(alive) >= 2
        if not ok:
            shortfall.append(country)
        log("  %-3s %4d %18d  %s" % (country, len(ids), len(alive), "OK" if ok else "不足"))
    if shortfall:
        log("  ⚠ 2媒体を下回った国: %s（feeds.json の代替候補を有効化してください）"
            % ", ".join(shortfall))
    else:
        log("  ✓ 全%d カ国で2媒体以上から記事を取得できました" % len(by_country))
    return shortfall


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="12カ国の複数媒体からRSSを取得して data/days/D.json に保存する")
    parser.add_argument("--feeds", dest="feeds_path", default=FEEDS_PATH,
                        help="フィード定義JSONのパス（既定: phase1/feeds.json）")
    parser.add_argument("--date", dest="date",
                        help="保存先の日付キー（YYYY-MM-DD）。既定はJSTの今日")
    parser.add_argument("--only", dest="only",
                        help="取得する feed_id をカンマ区切りで指定（動作確認用）")
    parser.add_argument("--limit", dest="limit", type=int,
                        help="先頭N本だけ取得する（動作確認用）")
    parser.add_argument("--offline", dest="offline_dir",
                        help="ネットに出ず DIR/<feed_id>.xml から読む（オフライン確認用）")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="取得はするが data/ に書かない")
    parser.add_argument("--no-wait", dest="no_wait", action="store_true",
                        help="レート待機を省く（オフライン確認用。本番では使わない）")
    args = parser.parse_args(argv)

    date_key = args.date or jst_today()
    try:
        feeds = load_feeds(args.feeds_path)
    except (OSError, ValueError) as e:
        print("feeds.json を読めませんでした: %s" % e)
        return 1

    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        feeds = [f for f in feeds if f["feed_id"] in wanted]
    if args.limit:
        feeds = feeds[:args.limit]
    if not feeds:
        print("取得対象のフィードが0本です（--only の指定か enabled を確認してください）")
        return 1

    wait = 0.0 if (args.no_wait or args.offline_dir) else INTER_FEED_WAIT_SEC
    throttle = None if (args.no_wait or args.offline_dir) else HostThrottle()

    print("=" * 72)
    print("フェーズ1 ①収集スクリプト")
    print("  対象日 (JST)  : %s" % date_key)
    print("  フィード定義  : %s" % args.feeds_path)
    print("  取得対象      : %d本 / %d カ国" % (len(feeds), len({f["country"] for f in feeds})))
    print("  schema_version: %d" % SCHEMA_VERSION)
    if args.offline_dir:
        print("  モード        : オフライン（%s）" % args.offline_dir)
    else:
        print("  タイムアウト  : %d秒 / 最大%d回試行" % (TIMEOUT_SEC, MAX_RETRY))
        print("  レート配慮    : 同一ホスト最低%.0f秒間隔・フィード間%.1f秒・UA明示"
              % (PER_HOST_MIN_INTERVAL_SEC, wait))
    print("=" * 72)

    started = time.monotonic()
    day_payload, summary = run(
        feeds, date_key, throttle=throttle, offline_dir=args.offline_dir,
        dry_run=args.dry_run, inter_feed_wait=wait)
    elapsed = time.monotonic() - started

    print("-" * 72)
    print("フィード別の取得結果（%d本 / %.1f秒）" % (len(feeds), elapsed))
    if day_payload:
        print("  %-16s %-26s %6s %-8s %s"
              % ("feed_id", "source", "件数", "status", "最新記事"))
        for s in day_payload["feeds"]:
            print("  %-16s %-26s %6d %-8s %s"
                  % (s["feed_id"], s["source"][:26], s["articles"], s["status"],
                     s["latest_published_at"] or "不明"))

    shortfall = print_country_coverage(feeds, day_payload["feeds"] if day_payload else [])

    print("-" * 72)
    print("総合状態: %s" % summary["status"])
    for alert in summary.get("alerts", []):
        print("  ⚠ %s (%s): %s" % (alert["source"], alert["status"], alert["note"]))

    if day_payload is None:
        print("結果: 失敗（全フィードから記事が取れませんでした。月別シャードに記録済み）")
        return 1

    print("記事 %d件 / %d カ国 / 正常フィード %d/%d本"
          % (summary["articles"], summary["countries"],
             summary["feeds_ok"], summary["feeds_total"]))
    if shortfall:
        # 2媒体を下回る国があっても保存自体は成功している。
        # 0で終わってCIを緑にし、警告はログとJSONに残す（毎日赤いと誰も見なくなる）。
        print("結果: 保存しましたが2媒体を下回った国があります（上の⚠を確認）")
        return 0
    print("結果: 成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
