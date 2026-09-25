#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini API の疎通診断。各モデルに「ok」とだけ返させるリクエストを1回ずつ送る。

使い方（GitHub Actions の「Gemini 疎通診断」ワークフローから手動で実行するのが簡単）:
    GEMINI_API_KEY=... python3 scripts/check_gemini.py
    python3 scripts/check_gemini.py --models gemini-3.5-flash-lite,gemini-3.8-flash

出力するのは「モデル名・HTTPコード・エラーの種類・所要時間・判定」だけ。
キーの値・エラー本文の文章は出さない（ログは公開リポジトリだと誰でも読める）。
消費するのは1モデルにつき1リクエスト（無料枠の日次上限にはほぼ影響しない）。
終了コード: 1つでも使えるモデルがあれば 0、全滅なら 1。
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich  # noqa: E402

ADVICE = {
    enrich.ERR_FATAL: "キーが無効か権限がありません。AI Studio でキーを作り直し、Secret を貼り直してください",
    enrich.ERR_MODEL_GONE: "このモデルは使えません（提供終了・日次上限・このプロジェクトでは利用不可）。他のモデルが使えれば問題ありません",
    enrich.ERR_OVERLOAD: "Google 側の一時的な混雑です。キーは正常。時間をおけば通ります",
    enrich.ERR_RATE: "毎分の上限に当たりました。キーは正常。1分おいて再実行してください",
    enrich.ERR_BAD_REQUEST: "このモデルが送った設定を受け付けませんでした（キーは正常）。他のモデルが✅なら問題ありません",
}


def probe(model, api_key, poster=None):
    poster = poster or enrich.http_post_json
    body = {
        "contents": [{"role": "user", "parts": [{"text": "Reply with the single word: ok"}]}],
        "generationConfig": {"maxOutputTokens": 256},
    }
    if not enrich.is_gemini3(model):
        body["generationConfig"]["temperature"] = 0
    thinking = enrich.thinking_config(model)
    if thinking:
        body["generationConfig"]["thinkingConfig"] = thinking
    url = "%s/%s:generateContent" % (enrich.API_BASE, model)
    t0 = time.monotonic()
    try:
        poster(url, body, api_key, timeout=60)
        return {"model": model, "ok": True, "status": 200, "kind": "",
                "label": "OK", "sec": time.monotonic() - t0}
    except enrich.ApiError as e:
        return {"model": model, "ok": False, "status": e.status,
                "kind": enrich.classify_error(e), "label": str(e),
                "sec": time.monotonic() - t0}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gemini API の疎通診断（1モデル1リクエスト）")
    parser.add_argument("--models", help="カンマ区切り（既定は enrich.py と同じ順番）")
    args = parser.parse_args(argv)
    models = enrich.configured_models(args.models.split(",") if args.models else None)
    api_key = enrich.read_api_key()
    if not api_key:
        print("GEMINI_API_KEY が設定されていません（Settings → Secrets and variables → Actions）")
        return 1
    print("キー: 設定あり（%d文字。値は表示しません）" % len(api_key))
    results = []
    for i, m in enumerate(models):
        if i:
            time.sleep(2)
        r = probe(m, api_key)
        results.append(r)
        mark = "✅" if r["ok"] else "❌"
        print("%s %-24s %-40s %.1f秒" % (mark, m, r["label"], r["sec"]))
        if not r["ok"]:
            print("     → %s" % ADVICE.get(r["kind"], "不明なエラー"))
        if r["kind"] == enrich.ERR_FATAL:
            print("キーの問題なので残りのモデルは試しません")
            break
    usable = [r["model"] for r in results if r["ok"]]
    print("")
    if usable:
        print("判定: 使えます（%s）。enrich.py はこの順で自動的に切り替えます" % ", ".join(usable))
        return 0
    if any(r["kind"] in (enrich.ERR_OVERLOAD, enrich.ERR_RATE) for r in results):
        print("判定: キーは正常ですが、今は混雑しています。30分ほどおいて再実行してください")
    else:
        print("判定: 使えるモデルがありません。上の → の対処をしてください")
    return 1


if __name__ == "__main__":
    sys.exit(main())
