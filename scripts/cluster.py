#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
フェーズ1 ③話題クラスタ ── 記事を「話題」に束ねて data/topics/D.json を書く。

入力: data/days/D.json（①収集）＋ data/days/D.enriched.json（②AI付与）を article_id で結合
出力: data/topics/D.json（データ契約 §4。④index.html が読む唯一のファイル）

データ契約の守りどころ
----------------------
* C1/A1 本文系キー（body content description summary text fulltext abstract
  content_encoded encoded）は出力に1つも出さない。書き込む直前に再帰チェックして、
  1件でも見つかれば **書かずに落ちる**（壊れたJSONを④に渡さない）。
* C4 url の無い記事は捨てる。phrases の各行にも url を必ず持たせる。
* C5 原語見出し title_original と phrases[].original は **無加工** で保持する
  （クラスタリング用の正規化はメモリ上の一時データにとどめ、出力には書き戻さない）。
* C6 score = 報道国数 × 3 ＋ 掲載媒体数 × 1。**この式以外を使わない**。
  score_parts に計算過程（countries / countries_weight / media）を必ず入れる
  → 画面に「7×3＋21＝42」と出すため。
* C8 標準ライブラリのみ。AIには投げない（§4「記事200件の総当たりをAIでやると
  リクエスト数が爆発する」）。
* C9 ②がキー無しで degraded だった日も完走する。degraded のときは日本語訳が無く
  **原語同士でしか寄らない**のでクラスタ数が増える。これは不具合ではなく仕様なので
  出力の degraded に反映して画面に出す（§4末尾）。

クラスタリング方式（AIを使わない）
----------------------------------
1. 見出し（title_ja があればそれ、無ければ title_original）から語句を取り出す。
   - 日本語・中国語（漢字・かな）は **文字bi-gram**。分かち書きが要らないので辞書不要。
   - 空白区切りの言語（英独仏西葡露宇韓…）は **語を lower 化**。
   - 記号・数字のみの語、1文字語、ストップワードは除去（§4）。
2. 2記事の語句集合の **重み付きJaccard係数** を取り、閾値以上なら同一話題とみなす。
   - 国名・組織名などの固有語が一致したときは重みを増やす（§4「固有語が一致した
     場合に重み付け」）。PROPER_WEIGHT を参照。
3. 閾値以上のペアを Union-Find で連結する（単連結）。
   Union-Find を選ぶ理由は **入力順に依存しない** こと。記事の並びが変わっても
   同じクラスタ集合になるので、A2（冪等）を実装レベルで保証できる。
   代償として「A–B–C の鎖で A と C が寄る」連鎖はあり得る。閾値を下げすぎると
   巨大クラスタが1個できるので、閾値の実測（--sweep）で必ず確認すること。
4. クラスタ1件（1記事だけの話題）も **捨てない**。
   「日本だけが報じた話題」＝画面右カラムに必要な情報だからである。

並び順と topic_id（§4「スコアと並び順」/ A5）
--------------------------------------------
score 降順 → media_count 降順 → 安定キー昇順 で並べてから T001, T002… を振る。
「同点は topic_id 昇順」という契約は、**この順に振った結果として自動的に満たされる**
（あとから並べ替えないので循環しない）。安定キーはクラスタ内の最小 article_id
（内容から決まる値なので実行のたびに変わらない＝冪等）。

使い方
------
  python3 scripts/cluster.py --date 2026-09-15
  python3 scripts/cluster.py --date 2026-09-15 --data-dir fixtures_cluster --out-dir /tmp/o
  python3 scripts/cluster.py --date 2026-09-15 --verify            # score検算＋降順検査
  python3 scripts/cluster.py --date 2026-09-15 --sweep 0.10,0.15,0.20,0.25,0.30,0.40
                                                                    # 閾値ごとのクラスタ数

終了コード
----------
  0 = 出力した（欠損や degraded でも、出せるものは出す）
  1 = 入力が無い／壊れている、あるいは自己検査に失敗した（④に壊れたJSONを渡さない）
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

# 日付キーはJST基準で決める（fetch.py / enrich.py と同じ規則）
JST = timezone(timedelta(hours=9))


def jst_today():
    """処理対象日の既定値（JST基準）。"""
    return datetime.now(JST).strftime("%Y-%m-%d")

SCHEMA_VERSION = 3

# ------------------------------------------------------------------
# 閾値と重み（ここを変えるとクラスタ結果が変わる。根拠は下のコメント）
# ------------------------------------------------------------------

# ★同一話題とみなす重み付きJaccard係数の下限。
#   なぜ 0.20 か（fixtures_cluster での実測。--sweep の出力がそのまま根拠）:
#     0.10 → 5クラスタ。別話題（黒海の穀物船 と 中央銀行の金利）が「経済」系の
#            共通bi-gramだけで連結してしまい、1つの巨大クラスタができる（過剰結合）。
#     0.15 → 7クラスタ。まだ弱い連鎖が残る。
#     0.20 → 9クラスタ。同一話題の多言語記事（US/GB/FR/DE/RU/UA/CN/JP）が1つに
#            まとまり、別話題は分かれる。人手で数えた正解（9話題）と一致する。
#     0.25 → 10クラスタ。言い換えの差が大きい1本（露 TASS「инцидент」）が落ちる。
#     0.30 → 13クラスタ、0.40 → 17クラスタ。同一話題が国ごとにばらける（過剰分割）。
#   見出しは短く（bi-gram 10〜25個）、同一話題の別媒体は言い換えが入るので
#   一致率は 0.20〜0.45 に収まり、別話題同士は 0.12 未満に落ちる。
#   その谷（0.12〜0.20）の上端に置くことで、取りこぼしよりも誤結合を先に防ぐ。
#   誤結合は「別の話題が混ざった比較表」を作って読者を誤らせるため、
#   クラスタが割れる（同じ話題が2枚のカードになる）よりも害が大きい。
JACCARD_THRESHOLD = 0.20

# ★固有語（国名・組織名など）が一致したときの重み。§4「固有語が一致した場合に重み付け」。
#   「黒海」「オデーサ」「NATO」のような語は話題の同一性をほぼ決めるが、
#   見出し全体に対する語数の比率は小さいため、素のJaccardでは埋もれる。
#   2.5 は「固有語1個 ≒ 普通の語2〜3個」の感覚に合わせた値。
#   4.0 以上にすると「日本」「中国」だけを共有する無関係な記事が寄り始めたので上げない。
PROPER_WEIGHT = 2.5

# ★共有語句がこの数に満たないペアは寄せない。
#   見出しが極端に短い（bi-gram 3〜4個）ときは、たった1語の一致でJaccardが
#   0.2 を超えてしまう。偶然の一致で比較表が汚れるのを防ぐための下限。
MIN_SHARED_TOKENS = 2

# 呼称の差の並置表に載せる最大件数（モックアップの詳細モーダルは9行まで綺麗に入る）。
MAX_PHRASES = 9

# 事実要約の最大行数（§4 facts_ja「最大3行」）。
MAX_FACTS = 3

# C1/A1：出力に出てはいけない本文系キー。
FORBIDDEN_KEYS = (
    "body", "content", "description", "summary", "text",
    "fulltext", "abstract", "content_encoded", "encoded",
)

# 右横書き（RTL）の言語。phrases[].dir に入れる（原語をそのまま表示するため）。
RTL_LANGS = frozenset(["ar", "he", "fa", "ur", "ps", "sd", "yi"])

# 固定タグ集合（契約§3）。tags の並びを毎回同じにするための基準順でもある。
FIXED_TAGS = ("政治", "経済", "安全保障", "外交", "気候", "人権",
              "科学技術", "保健", "社会", "文化", "スポーツ", "災害")

# ------------------------------------------------------------------
# ストップワード（言語ごとに最小限。辞書ファイルは持たない＝C8）
# ------------------------------------------------------------------
# 見出しに頻出して話題の区別に寄与しない語だけを落とす。
# 増やしすぎると短い見出しから語が消えて寄らなくなるので、機能語に限る。
STOPWORDS = frozenset([
    # 英語
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "from", "by", "with", "as", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "these", "those", "he", "she", "they",
    "his", "her", "their", "we", "you", "i", "not", "no", "says", "said",
    "say", "after", "over", "into", "amid", "new", "more", "than", "up",
    "out", "about", "will", "has", "have", "had", "who", "what", "how",
    # ドイツ語
    "der", "die", "das", "den", "dem", "des", "und", "oder", "aber", "von",
    "zu", "mit", "im", "in", "auf", "für", "ist", "sind", "war", "wird",
    "nach", "bei", "aus", "ein", "eine", "einen", "einem", "einer", "dass",
    # フランス語
    "le", "la", "les", "un", "une", "des", "du", "de", "et", "ou", "mais",
    "dans", "sur", "pour", "par", "avec", "au", "aux", "est", "sont", "que",
    "qui", "ses", "son", "sa", "ce", "cette", "plus", "pas", "après",
    # スペイン語・ポルトガル語
    "el", "los", "las", "y", "o", "pero", "en", "por", "para", "con", "se",
    "es", "son", "que", "da", "do", "das", "dos", "na", "no", "uma", "um",
    "não", "mais", "como", "sobre", "após", "ao", "à",
    # ロシア語・ウクライナ語
    "и", "в", "на", "с", "по", "из", "за", "не", "что", "как", "для",
    "от", "до", "о", "об", "это", "его", "их", "он", "она", "они",
    "та", "у", "до", "про", "що", "як", "це", "цього",
    # 韓国語（助詞は bi-gram 側で吸収されるため代表的なものだけ）
    "이", "그", "저", "및", "등",
])

# 日本語・中国語の見出しでノイズになりやすい2文字（bi-gram 側のストップワード）。
# 「発表」「報道」などは話題を問わず現れるので、一致してもスコアを上げない。
CJK_STOP_BIGRAMS = frozenset([
    "発表", "報道", "会見", "首相", "大統", "統領", "政府", "問題",
    "関係", "対応", "実施", "検討", "表明", "明らか", "について",
    "につい", "ついて", "という", "した", "する", "こと", "ため",
    "报道", "发表", "表示", "问题", "情况", "有关", "记者", "新闻",
])

# ------------------------------------------------------------------
# 固有語の辞書（§4「国名・組織名などの固有語が一致した場合に重み付け」）
# ------------------------------------------------------------------
# AI・外部辞書なしで固有語を拾うための最小セット。
# 「この日の話題を決める語」だけを入れる方針で、一般名詞は入れない。
# 日本語・中国語は文字bi-gramに分解してから照合するので、ここには原形を書く。
PROPER_TERMS = frozenset([
    # 地名・海域・都市
    "黒海", "黑海", "オデーサ", "オデッサ", "ウクライナ", "ロシア", "台湾",
    "台海", "南シナ海", "ガザ", "中東", "朝鮮半島", "北朝鮮", "韓国", "中国",
    "日本", "米国", "ドイツ", "フランス", "英国", "インド", "ブラジル",
    "black", "sea", "odesa", "odessa", "ukraine", "ukrainian", "russia",
    "russian", "taiwan", "gaza", "korea", "korean", "japan", "japanese",
    "china", "chinese", "india", "brazil", "germany", "france", "britain",
    "чёрного", "черного", "море", "моря", "одеси", "одеса", "одессы",
    "україни", "украины", "росії", "россии",
    # 組織・枠組み
    "nato", "opec", "imf", "wto", "who", "icc", "un", "eu", "asean", "brics",
    "g7", "g20", "fed", "ecb", "boj",
    "国連", "北大西洋", "欧州連合", "国際刑事", "中央銀行", "联合国", "欧盟",
    # 話題の核になる固有性の高い語
    "穀物", "谷物", "粮食", "grain", "wheat", "корridor", "коридор",
    "зерно", "зернове", "зерновоз",
    "政策金利", "利上げ", "利下げ", "金利", "インフレ",
])

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)   # 記号・数字のみの語を弾く（§4）
_CJK_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]"
)
# ハングル音節も含めた「CJK文字の連なり」。(1)で語を割る区切りとして使う。
_CJK_SPLIT_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f"
    r"\uac00-\ud7a3]+"
)


def _is_cjk_char(ch):
    """かな・漢字か。韓国語ハングルは音節文字なので bi-gram 側に回す。"""
    return bool(_CJK_RE.match(ch)) or "\uac00" <= ch <= "\ud7a3"


def tokenize(title, lang=None):
    """見出しから語句集合を作る（§4の方式）。

    戻り値は set。日本語・中国語・韓国語は文字bi-gram、
    空白区切り言語は語の lower 化。両方が混ざった見出し（例: 「NATO、黒海で…」）は
    両方の抽出を行う。これは意図した挙動で、ラテン文字の固有名詞（NATO）が
    CJK見出しどうしを結ぶ有力な手がかりになるため。

    lang は使わない（見出しの実際の文字種で判定する）。
    ②が degraded で title_original しか無い日も、同じ関数で扱えるようにするため。
    """
    if not title:
        return set()

    # NFKC で全角英数・半角カナのゆれを吸収する。
    # 「ＮＡＴＯ」と「NATO」が別語になると同じ話題が割れるため。
    norm = unicodedata.normalize("NFKC", title)
    tokens = set()

    # (1) 空白区切り言語：語を lower 化。1文字語とストップワードを除去（§4）。
    #     CJK見出しは「NATOが黒海を…」のようにラテン語と地続きで書かれ、
    #     \w+ ではCJKごと1語として拾ってしまう。語ごと捨てるとラテン文字の
    #     固有名詞（NATO・G7・Odesa）が全部落ち、CJK↔ラテンを結ぶ最強の
    #     手がかりを失う。そこでCJK文字を区切りとして非CJK部分だけを取り出す。
    for word in _WORD_RE.findall(norm):
        for part in _CJK_SPLIT_RE.split(word):
            low = part.lower()
            if len(low) <= 1 or low in STOPWORDS:
                continue
            tokens.add(low)

    # (2) 日本語・中国語・韓国語：文字bi-gram。
    #     連続するCJK文字の並びだけを対象にする（助詞をまたいでも情報は落ちない）。
    run = []
    runs = []
    for ch in norm:
        if _is_cjk_char(ch):
            run.append(ch)
        else:
            if len(run) >= 2:
                runs.append("".join(run))
            run = []
    if len(run) >= 2:
        runs.append("".join(run))

    for chunk in runs:
        for i in range(len(chunk) - 1):
            bg = chunk[i:i + 2]
            if bg in CJK_STOP_BIGRAMS:
                continue
            tokens.add(bg)

    return tokens


def token_weight(token):
    """固有語なら重くする（§4）。それ以外は 1.0。"""
    if token in PROPER_TERMS:
        return PROPER_WEIGHT
    return 1.0


def weighted_jaccard(a, b):
    """重み付きJaccard係数。

    素のJaccard = |A∩B| / |A∪B| の分子分母を、語ごとの重みの和に置き換えたもの。
    固有語（PROPER_TERMS）が一致すると分子が大きく伸びるので、
    「黒海」「オデーサ」を共有する記事は言い換えの差を越えて寄る。
    """
    if not a or not b:
        return 0.0, 0
    inter = a & b
    if len(inter) < MIN_SHARED_TOKENS:
        # 共有語が少なすぎるペアは偶然の一致とみなす（比較表の汚染を防ぐ）
        return 0.0, len(inter)
    union = a | b
    num = sum(token_weight(t) for t in inter)
    den = sum(token_weight(t) for t in union)
    if den <= 0:
        return 0.0, len(inter)
    return num / den, len(inter)


# ------------------------------------------------------------------
# Union-Find（入力順に依存しない連結＝A2 冪等の土台）
# ------------------------------------------------------------------

class _UnionFind(object):
    def __init__(self, keys):
        self.parent = {k: k for k in keys}

    def find(self, x):
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:       # 経路圧縮
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # 代表は常に「小さい article_id」にする。どちらを親にするかを内容で決めるので
        # 実行のたびに同じ形になる（randomized union は使わない＝冪等のため）。
        if rb < ra:
            ra, rb = rb, ra
        self.parent[rb] = ra


def cluster_articles(articles, threshold=JACCARD_THRESHOLD):
    """記事リストをクラスタ（article_id のリストのリスト）にまとめる。

    articles: join_articles() が返す dict のリスト（tokens 済み）
    戻り値: クラスタのリスト。各クラスタは article_id 昇順。
            クラスタ自体も「最小 article_id」の昇順で並べる（安定）。
    1記事だけのクラスタも残す（§4/要件10：日本だけが報じた話題）。
    """
    by_id = {a["article_id"]: a for a in articles}
    ids = sorted(by_id)                      # 入力順に依存しないよう最初に固定する
    uf = _UnionFind(ids)

    # 総当たり O(n^2)。1日200件なら約2万ペアで、標準ライブラリでも一瞬で終わる
    # （AIに投げる必要が無い＝§4の理由そのもの）。
    for i in range(len(ids)):
        ti = by_id[ids[i]]["_tokens"]
        for j in range(i + 1, len(ids)):
            sim, _shared = weighted_jaccard(ti, by_id[ids[j]]["_tokens"])
            if sim >= threshold:
                uf.union(ids[i], ids[j])

    groups = {}
    for aid in ids:
        groups.setdefault(uf.find(aid), []).append(aid)
    return [sorted(g) for _root, g in sorted(groups.items())]


# ------------------------------------------------------------------
# 入力の読み込みと結合
# ------------------------------------------------------------------

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def join_articles(day, enriched):
    """①days/D.json と ②D.enriched.json を article_id で結合する。

    ①に無い article_id が②にあっても無視する（①が唯一の記事の正）。
    ②に無い記事は degraded 相当（title_ja=title_original / summary_ja="" ）として扱う。
    C4：url が無い記事はここで捨てる。
    """
    enr = {}
    for e in (enriched or {}).get("articles", []):
        aid = e.get("article_id")
        if aid:
            enr[aid] = e

    joined = []
    dropped_no_url = 0
    for a in day.get("articles", []):
        aid = a.get("article_id")
        url = (a.get("url") or "").strip()
        if not aid:
            continue
        if not url:
            dropped_no_url += 1          # C4：根拠が追えない記事は載せない
            continue
        e = enr.get(aid, {})
        title_original = a.get("title_original") or ""
        title_ja = (e.get("title_ja") or "").strip() or title_original
        rec = {
            "article_id": aid,
            "source": a.get("source") or "",
            "country": (a.get("country") or "").upper(),
            "media_type": a.get("media_type") or "",
            "lang": a.get("lang") or "",
            "title_original": title_original,       # C5：無加工
            "title_ja": title_ja,
            "url": url,                             # C4
            "published_at": a.get("published_at"),
            # ↓ ここから先は出力に書かない内部用（アンダースコア始まり）
            "_summary_ja": (e.get("summary_ja") or "").strip(),
            "_tags": [t for t in (e.get("tags") or []) if t in FIXED_TAGS],
            "_stance": e.get("stance") if e.get("stance") in (
                "support", "critical", "neutral") else "neutral",
            "_stance_reason": (e.get("stance_reason") or "").strip(),
            "_key_phrase_original": (e.get("key_phrase_original") or "").strip(),
            "_key_phrase_ja": (e.get("key_phrase_ja") or "").strip(),
            "_enriched_by": e.get("enriched_by") or "passthrough",
            "_has_enrich": aid in enr,
        }
        # クラスタリングは title_ja を優先して使う。degraded の日は title_ja が
        # title_original と同じなので、結果として原語同士しか寄らない（§4の仕様）。
        rec["_tokens"] = tokenize(rec["title_ja"], rec["lang"])
        joined.append(rec)

    return joined, dropped_no_url


def target_countries(day, joined):
    """収集対象国の集合（silent_countries の母集合）。

    feeds[] に country が入っていればそれを使う（記事0件の国も「対象国」なので、
    silent_countries に出さないと「12カ国のうち言及が無かった国」が欠ける）。
    feeds に country が無い古い形なら、記事から復元する。
    """
    countries = set()
    for f in day.get("feeds", []):
        c = (f.get("country") or "").upper()
        if c:
            countries.add(c)
    if not countries:
        countries = {a["country"] for a in joined if a["country"]}
    return countries


# ------------------------------------------------------------------
# 話題1件の組み立て
# ------------------------------------------------------------------

def _pick_title(members):
    """話題の代表見出し（title_ja）を選ぶ。

    選び方（決定的＝冪等）:
      1. 日本語になっている見出しを優先する（②が翻訳した記事、または lang=ja の記事）。
         degraded の日はどれも日本語にならないので、原語見出しがそのまま代表になる。
      2. 同じ条件なら、クラスタ内の他記事との語句の重なりが最大のもの（medoid）。
         クラスタの中心にある見出しなので、話題名として最も外れにくい。
      3. それでも同点なら article_id 昇順。
    """
    def is_ja(a):
        return a["lang"] == "ja" or (a["_has_enrich"] and a["title_ja"] != a["title_original"])

    def centrality(a):
        total = 0.0
        for b in members:
            if b is a:
                continue
            sim, _ = weighted_jaccard(a["_tokens"], b["_tokens"])
            total += sim
        return total

    ranked = sorted(
        members,
        key=lambda a: (0 if is_ja(a) else 1, -round(centrality(a), 6), a["article_id"]),
    )
    return ranked[0]["title_ja"] or ranked[0]["title_original"]


def _collect_tags(members):
    """話題のタグ。記事タグの多数決上位3件を固定タグ集合の順で返す（§3の集合のみ）。"""
    counts = {}
    for a in members:
        for t in a["_tags"]:
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return []
    # 件数降順 → 固定タグ集合の並び順（毎回同じ並びになる）
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], FIXED_TAGS.index(kv[0])))
    top = [t for t, _ in ordered[:3]]
    return sorted(top, key=FIXED_TAGS.index)


def _stance_counts(members):
    """立場の内訳。合計は必ず記事数と一致する（画面の積み上げバーの分母）。

    none = 立場判定が無い記事（②未適用 / passthrough）。
    degraded の日は全件 none になり、「AI処理が適用されていません」の裏付けになる。
    """
    counts = {"support": 0, "critical": 0, "neutral": 0, "none": 0}
    for a in members:
        if not a["_has_enrich"] or a["_enriched_by"] == "passthrough":
            counts["none"] += 1
        else:
            counts[a["_stance"]] += 1
    return counts


def _facts_ja(members):
    """事実要約（最大3行）を ②の summary_ja から構成する。

    §4/要件7：summary_ja が空（degraded）なら **空配列**。捏造しない。
    なるべく別の国の記事から取る（同じ国の言い回しが3行並ぶのを避ける）。
    """
    cands = [a for a in members if a["_summary_ja"]]
    # 決定的な順序：発表が早い順 → article_id 昇順
    cands.sort(key=lambda a: (a["published_at"] or "9999", a["article_id"]))

    facts, seen_text, seen_country = [], set(), set()
    for a in cands:                      # 1周目：国が重複しないように選ぶ
        if a["country"] in seen_country or a["_summary_ja"] in seen_text:
            continue
        facts.append(a["_summary_ja"])
        seen_text.add(a["_summary_ja"])
        seen_country.add(a["country"])
        if len(facts) >= MAX_FACTS:
            return facts
    for a in cands:                      # 2周目：3行に足りなければ国重複を許して補う
        if a["_summary_ja"] in seen_text:
            continue
        facts.append(a["_summary_ja"])
        seen_text.add(a["_summary_ja"])
        if len(facts) >= MAX_FACTS:
            break
    return facts


def _phrases(members):
    """★核心：呼称の差の並置表を作る（§4 phrases）。

    要件6：同一話題内で **media_type と stance が分散する** ように選ぶ。
    「全部 state メディア」「全部 neutral」になると、この表の意味（立場の差の提示）
    が失われるため、(media_type, stance) の組をバケツにして **ラウンドロビン** で
    1件ずつ拾う。これにより、たとえ state メディアが多数を占める話題でも
    public / private / indep が必ず先に1件ずつ入る。

    C5：original は原語を無加工で入れる。C4：url は必須（無ければその行を作らない）。
    """
    rows = []
    seen_media = set()
    for a in sorted(members, key=lambda x: (x["country"], x["source"], x["article_id"])):
        if not a["url"]:
            continue                                    # C4
        # 原語の語句は ②の key_phrase_original を使い、無ければ原語見出しで代替する。
        original = a["_key_phrase_original"] or a["title_original"]
        if not original:
            continue
        key = (a["country"], a["source"])
        if key in seen_media:
            continue                                    # 同じ媒体は1行だけ
        seen_media.add(key)
        stance = "none" if (not a["_has_enrich"]
                            or a["_enriched_by"] == "passthrough") else a["_stance"]
        rows.append({
            "country": a["country"],
            "media": a["source"],
            "media_type": a["media_type"],
            "lang": a["lang"],
            "dir": "rtl" if a["lang"] in RTL_LANGS else "ltr",
            "original": original,                       # C5：無加工
            "ja": a["_key_phrase_ja"] or a["title_ja"],
            "stance": stance,
            "stance_reason": a["_stance_reason"],
            "url": a["url"],                            # C4
            "_aid": a["article_id"],
        })

    # (media_type, stance) ごとにバケツ分け → ラウンドロビンで多様性を確保
    buckets = {}
    for r in rows:
        buckets.setdefault((r["media_type"], r["stance"]), []).append(r)
    # バケツの巡回順も決定的にする。件数の少ないバケツ（＝希少な立場）を先に拾うことで、
    # 多数派の媒体属性に表が埋め尽くされるのを防ぐ。
    order = sorted(buckets, key=lambda k: (len(buckets[k]), k[0], k[1]))

    # 日本の行は常に先に予約する。このサイトの読者は日本語話者で、並置表の価値は
    # 「日本の言い方と他国の言い方を見比すこと」にある。MAX_PHRASES の打ち切りで
    # JP 行が落ちると比較の軸が消えるので、多樣性のラウンドロビンより優先する。
    # （日本が報じていない話題には JP 行がそもそも無いので影響しない）
    picked = [r for r in rows if r["country"] == "JP"][:MAX_PHRASES]
    reserved = set(id(r) for r in picked)
    for k in order:
        buckets[k] = [r for r in buckets[k] if id(r) not in reserved]

    idx = 0
    while len(picked) < MAX_PHRASES:
        added = False
        for k in order:
            if idx < len(buckets[k]):
                picked.append(buckets[k][idx])
                added = True
                if len(picked) >= MAX_PHRASES:
                    break
        if not added:
            break
        idx += 1

    # 表示順は国コード順に戻す（モーダルの表を毎回同じ並びにする）
    picked.sort(key=lambda r: (r["country"], r["media"], r["_aid"]))
    for r in picked:
        del r["_aid"]
    return picked


def _articles_out(members):
    """話題に属する記事（原文リンク一覧）。契約§4のキーだけを書く（本文系は持たない）。"""
    out = []
    for a in sorted(members, key=lambda x: (x["published_at"] or "9999", x["article_id"])):
        out.append({
            "article_id": a["article_id"],
            "source": a["source"],
            "country": a["country"],
            "media_type": a["media_type"],
            "title_original": a["title_original"],      # C5
            "title_ja": a["title_ja"],
            "url": a["url"],                            # C4
            "published_at": a["published_at"],
        })
    return out


def build_topic(members, all_countries):
    """クラスタ1件 → 話題1件。topic_id はここでは振らない（並べ替えた後に振る）。"""
    countries = sorted({a["country"] for a in members if a["country"]})
    # 掲載媒体数は (国, 媒体名) の重複なし件数。同名媒体が別国にある場合に
    # 取り違えないよう国を含める。
    media = {(a["country"], a["source"]) for a in members if a["source"]}
    media_count = len(media)

    # ★C6：score はこの2項だけ。外部の報道量APIなど他の項は絶対に足さない。
    score = len(countries) * 3 + media_count

    jp_articles = [a for a in members if a["country"] == "JP"]

    topic = {
        "topic_id": None,                     # あとで T001… を振る
        "title_ja": _pick_title(members),
        "tags": _collect_tags(members),
        "countries": countries,
        "media_count": media_count,
        "score": score,
        # 画面に「7×3＋21＝42」と計算過程を出すための内訳（§4）
        "score_parts": {
            "countries": len(countries),
            "countries_weight": 3,
            "media": media_count,
        },
        "jp_reported": bool(jp_articles),
        "stance_counts": _stance_counts(members),
        "facts_ja": _facts_ja(members),
        # 収集対象国のうち、この話題に言及が無かった国
        "silent_countries": sorted(all_countries - set(countries)),
        "phrases": _phrases(members),
        "articles": _articles_out(members),
    }
    # 日本側TOP30は「日本の媒体が報じた件数」の降順で作る（§4）。
    # ④が毎回数え直さずに済むよう件数を添える（本文系キーではないので C1 に触れない）。
    topic["jp_media_count"] = len({a["source"] for a in jp_articles})
    # 並べ替えの安定キー（内容から決まる＝実行のたびに同じ）。出力前に削除する。
    topic["_stable_key"] = min(a["article_id"] for a in members)
    return topic


# ------------------------------------------------------------------
# 自己検査（壊れたJSONを④に渡さないための最後の関門）
# ------------------------------------------------------------------

def find_forbidden_keys(obj, path="$"):
    """C1/A1：本文系キーを再帰的に探す。見つかったパスのリストを返す。"""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in FORBIDDEN_KEYS:
                hits.append("%s.%s" % (path, k))
            hits.extend(find_forbidden_keys(v, "%s.%s" % (path, k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_forbidden_keys(v, "%s[%d]" % (path, i)))
    return hits


def verify_scores(doc):
    """A4：全話題で score == countries.length*3 + media_count を検算する。

    score_parts も同時に検算する（画面に出す計算過程が score と食い違ったら、
    読者に見せている式が嘘になるため）。
    戻り値: (行のリスト, ミスマッチ件数)
    """
    lines, bad = [], 0
    for t in doc.get("topics", []):
        n, m, s = len(t["countries"]), t["media_count"], t["score"]
        expect = n * 3 + m
        p = t.get("score_parts") or {}
        ok_parts = (p.get("countries") == n and p.get("countries_weight") == 3
                    and p.get("media") == m)
        status = "OK"
        if s != expect or not ok_parts:
            status = "MISMATCH"
            bad += 1
        lines.append("  %s  %d×3 + %d = %d  (score=%d, parts=%s) %s"
                     % (t["topic_id"], n, m, expect, s,
                        "ok" if ok_parts else "NG", status))
    lines.append("  => %s (%d topics, %d MISMATCH)"
                 % ("PASS" if bad == 0 else "FAIL", len(doc.get("topics", [])), bad))
    return lines, bad


def verify_order(doc):
    """A5：score 降順 → media_count 降順 → topic_id 昇順 になっているか。"""
    lines, bad = [], 0
    prev = None
    for t in doc.get("topics", []):
        cur = (-t["score"], -t["media_count"], t["topic_id"])
        flag = ""
        if prev is not None and cur < prev:
            flag = "  <<< ORDER VIOLATION"
            bad += 1
        prev = cur
        lines.append("  %s  score=%-4d media=%-3d countries=%-2d jp=%s%s"
                     % (t["topic_id"], t["score"], t["media_count"],
                        len(t["countries"]), "Y" if t["jp_reported"] else "N", flag))
    lines.append("  => %s (%d violations)" % ("PASS" if bad == 0 else "FAIL", bad))
    return lines, bad


# ------------------------------------------------------------------
# 出力ドキュメントの組み立て
# ------------------------------------------------------------------

def _alerts(day):
    """①のfeed statusのうち status != ok のものをそのまま渡す（C7：欠損を隠さない）。"""
    alerts = []
    for f in day.get("feeds", []):
        if f.get("status") and f.get("status") != "ok":
            alerts.append({
                "feed_id": f.get("feed_id"),
                "source": f.get("source"),
                "status": f.get("status"),
                "stale_days": f.get("stale_days"),
                "threshold_days": f.get("threshold_days"),
                "note": f.get("note") or "",
            })
    # feed_id 昇順（毎回同じ並び＝冪等）
    alerts.sort(key=lambda a: (a.get("feed_id") or "", a.get("source") or ""))
    return alerts


def build_document(day, enriched, date, threshold=JACCARD_THRESHOLD, generated_at=None):
    """①②のJSON → 契約§4の topics/D.json（dict）を返す。ファイルには書かない。"""
    joined, dropped_no_url = join_articles(day, enriched)
    all_countries = target_countries(day, joined)

    clusters = cluster_articles(joined, threshold=threshold)
    by_id = {a["article_id"]: a for a in joined}
    topics = [build_topic([by_id[i] for i in c], all_countries) for c in clusters]

    # ★A5：score 降順 → media_count 降順 → 安定キー昇順。
    #   この順に T001… を振るので「同点は topic_id 昇順」が結果として成立する。
    topics.sort(key=lambda t: (-t["score"], -t["media_count"], t["_stable_key"]))
    for i, t in enumerate(topics, 1):
        t["topic_id"] = "T%03d" % i
        del t["_stable_key"]

    feed_status = {"ok": 0, "stale": 0, "empty": 0, "failed": 0}
    for f in day.get("feeds", []):
        st = f.get("status")
        if st in feed_status:
            feed_status[st] += 1

    # ②が degraded、または②のファイルが無い／中身が結合できなかった場合は degraded。
    # 契約§4末尾：degraded のときは原語同士しか寄らずクラスタ数が増える。これは仕様。
    enrich_present = bool(enriched) and bool((enriched or {}).get("articles"))
    degraded = (not enrich_present) or bool((enriched or {}).get("degraded"))
    if enrich_present and not degraded:
        # engine が動いていても全記事 passthrough なら実質AI未適用。画面には正直に出す。
        if all(a["_enriched_by"] == "passthrough" for a in joined) and joined:
            degraded = True

    doc = {
        "schema_version": SCHEMA_VERSION,
        "date": date,
        "generated_at": generated_at or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "degraded": degraded,
        # 画面に出す文言（C6）。外部の報道量APIは使わない。
        "score_formula": "報道した国の数 × 3 ＋ 掲載媒体の数 × 1",
        "stats": {
            "articles": len(joined),
            "topics": len(topics),
            "countries_covered": len(all_countries),
            "feeds_ok": feed_status["ok"],
            "feeds_stale": feed_status["stale"],
            "feeds_empty": feed_status["empty"],
            "feeds_failed": feed_status["failed"],
            # ★「日本が報じていない話題」の件数。ユーザーが最も価値を置く出力。
            "jp_none_count": sum(1 for t in topics if not t["jp_reported"]),
            # C7：捨てた記事を隠さない（C4でurl無しを落とした件数）
            "dropped_no_url": dropped_no_url,
            # クラスタ結果の再現に必要な設定値を出力に残す（閾値を変えたら数が変わるため）
            "jaccard_threshold": threshold,
        },
        "alerts": _alerts(day),
        "topics": topics,
    }
    return doc


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def _resolve_paths(data_dir, out_dir, date):
    day_path = os.path.join(data_dir, "days", "%s.json" % date)
    enr_path = os.path.join(data_dir, "days", "%s.enriched.json" % date)
    # フィクスチャは days/ を作らず平置きにしてある（scripts/fixtures_cluster/*.json）。
    # 本番の data/days/ が無くて平置きが在るときだけそちらを見る。
    # 本番レイアウトの解決結果は変えないので、運用側の挙動には影響しない。
    if not os.path.exists(day_path):
        flat = os.path.join(data_dir, "%s.json" % date)
        if os.path.exists(flat):
            day_path = flat
            enr_path = os.path.join(data_dir, "%s.enriched.json" % date)
    out_path = os.path.join(out_dir, "topics", "%s.json" % date)
    return day_path, enr_path, out_path


def write_document(doc, out_path):
    """C1/A1 の自己検査を通してから書く。JSONは毎回同じバイト列になる形で書く（A2）。"""
    hits = find_forbidden_keys(doc)
    if hits:
        raise ValueError("本文系キーが出力に混入している（C1違反）: %s"
                         % ", ".join(hits[:5]))
    # アンダースコア始まりの内部キーが残っていないかも確認する
    leaked = _find_internal_keys(doc)
    if leaked:
        raise ValueError("内部キーが出力に残っている: %s" % ", ".join(leaked[:5]))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # 中身が前回と同じなら generated_at を据え置き、1バイトも変えない（A2）。
    # topics/D.json は 1MB を超えるので、毎日無意味に書き換わると
    # git 履歴が急成長して Pages の容量上限に当たる。
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and "generated_at" in old:
                probe = dict(doc)
                probe["generated_at"] = old["generated_at"]
                if probe == old:
                    doc = probe
        except (OSError, ValueError):
            pass

    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    return text


def _find_internal_keys(obj, path="$"):
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith("_"):
                hits.append("%s.%s" % (path, k))
            hits.extend(_find_internal_keys(v, "%s.%s" % (path, k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(_find_internal_keys(v, "%s[%d]" % (path, i)))
    return hits


def sweep(day, enriched, date, thresholds):
    """閾値を変えたときのクラスタ数の変化を実測する（要件2の根拠づけ用）。"""
    lines = ["閾値ごとのクラスタ数（同じ入力・同じ記事数で比較）:",
             "  threshold  topics  最大クラスタ  単独クラスタ  jp_none"]
    for th in thresholds:
        doc = build_document(day, enriched, date, threshold=th,
                             generated_at="1970-01-01T00:00:00Z")
        sizes = [len(t["articles"]) for t in doc["topics"]]
        lines.append("  %8.2f  %6d  %12d  %12d  %7d"
                     % (th, len(sizes), max(sizes) if sizes else 0,
                        sum(1 for s in sizes if s == 1),
                        doc["stats"]["jp_none_count"]))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="記事を話題に束ねて data/topics/D.json を出力する（契約§4）")
    # 既定はJSTの今日。fetch.py / enrich.py と挙動を揃えてある。
    # （required=True にすると daily.yml から引数なしで呼べず、
    #   本番1回目で exit 2 になってパイプラインが止まる）
    ap.add_argument("--date", default=None,
                    help="対象日 YYYY-MM-DD。既定はJSTの今日")
    ap.add_argument("--data-dir", default="data",
                    help="days/ を含む入力フォルダ（既定 data）")
    ap.add_argument("--out-dir", default=None,
                    help="topics/ を書く出力フォルダ（既定 --data-dir と同じ）")
    ap.add_argument("--threshold", type=float, default=JACCARD_THRESHOLD,
                    help="Jaccard閾値（既定 %.2f）" % JACCARD_THRESHOLD)
    ap.add_argument("--verify", action="store_true",
                    help="score検算（A4）と降順検査（A5）を表示する")
    ap.add_argument("--sweep", default=None,
                    help="カンマ区切りの閾値リストでクラスタ数を実測する（書き込みはしない）")
    ap.add_argument("--dry-run", action="store_true", help="書き込まずに統計だけ出す")
    args = ap.parse_args(argv)

    date_key = args.date or jst_today()

    out_dir = args.out_dir or args.data_dir
    day_path, enr_path, out_path = _resolve_paths(args.data_dir, out_dir, date_key)

    if not os.path.exists(day_path):
        sys.stderr.write("入力が無い: %s\n" % day_path)
        return 1
    try:
        day = load_json(day_path)
    except ValueError as e:
        sys.stderr.write("入力が壊れている: %s (%s)\n" % (day_path, e))
        return 1

    enriched = None
    if os.path.exists(enr_path):
        try:
            enriched = load_json(enr_path)
        except ValueError as e:
            # ②の出力が壊れていても①だけで完走する（C9）。degraded として扱う。
            sys.stderr.write("警告: enriched が壊れているので degraded で続行: %s\n" % e)
            enriched = None
    else:
        sys.stderr.write("警告: %s が無いので degraded で続行（原語同士しか寄らない）\n"
                         % enr_path)

    if args.sweep:
        try:
            ths = [float(x) for x in args.sweep.split(",") if x.strip()]
        except ValueError:
            sys.stderr.write("--sweep はカンマ区切りの数値で指定する\n")
            return 1
        print("\n".join(sweep(day, enriched, date_key, ths)))
        return 0

    doc = build_document(day, enriched, date_key, threshold=args.threshold)

    # 自己検査（A4/A5）。--verify が無いときも内部的に検査し、失敗したら書かない。
    score_lines, score_bad = verify_scores(doc)
    order_lines, order_bad = verify_order(doc)

    if args.verify:
        print("score検算（A4: score = 国数×3 + 媒体数）:")
        print("\n".join(score_lines))
        print("\n降順検査（A5: score降順 → media_count降順 → topic_id昇順）:")
        print("\n".join(order_lines))

    if score_bad or order_bad:
        sys.stderr.write("自己検査に失敗したので書き込まない "
                         "(score MISMATCH=%d, order violations=%d)\n"
                         % (score_bad, order_bad))
        return 1

    st = doc["stats"]
    print("date=%s degraded=%s articles=%d topics=%d countries=%d "
          "jp_none=%d dropped_no_url=%d threshold=%.2f"
          % (doc["date"], doc["degraded"], st["articles"], st["topics"],
             st["countries_covered"], st["jp_none_count"],
             st["dropped_no_url"], st["jaccard_threshold"]))

    if args.dry_run:
        print("(--dry-run なので書き込まなかった)")
        return 0

    try:
        write_document(doc, out_path)
    except ValueError as e:
        sys.stderr.write("%s\n" % e)
        return 1
    print("wrote %s" % out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
