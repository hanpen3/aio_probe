#!/usr/bin/env python3
"""
gsb_check.py：html_probe.py が取り出したリンク（out/links.tsv）を、Google Safe Browsing（v4 Lookup API）で調べる。

■ 使い方
  python3 gsb_check.py                 # out/links.tsv を読んで調べ、out/gsb_results.tsv と report.html を更新する
  python3 gsb_check.py --out 別のフォルダ
  python3 gsb_check.py --dry-run       # 問い合わせずに、送る件数だけ表示する
  python3 gsb_check.py --key-file ~/work/.gsb_key   # キーのファイルを指定する

■ API キー
  環境変数 GSB_API_KEY、またはファイル（既定 ~/.gsb_key。キーの文字列だけを1行）。キーは画面にもファイルにも出さない

■ 送るもの
  links.tsv の gsb_url 列（「スキーム＋ホスト名＋パス」。クエリと # は送らない）を、重複を除いて 500 件ずつ。
  脅威の種類：MALWARE / SOCIAL_ENGINEERING / UNWANTED_SOFTWARE / POTENTIALLY_HARMFUL_APPLICATION、プラットフォーム：ANY_PLATFORM
  ※ 判定は「問い合わせた時点」のもの。実行するたびに全部を調べ直し、gsb_results.tsv を上書きする

■ 保存するもの（--out のフォルダ）
  gsb_results.tsv  送った URL ごとの結果（gsb_url、判定（無ければ空）、問い合わせた日時（UTC））
  report.html      GSB の結果を入れて作り直す

■ 無料。営利目的でない用途に限る。1日の上限などは Google Cloud のコンソールで確認する
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from html_probe import make_report, read_tsv

ENDPOINT = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
THREAT_TYPES = ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"]
BATCH = 500


def read_key(path):
    v = os.environ.get("GSB_API_KEY", "").strip()
    if v:
        return v
    p = os.path.expanduser(path)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip() or None
    return None


def ask(key, urls, endpoint):
    body = {"client": {"clientId": "wd-hackathon", "clientVersion": "1.0"},
            "threatInfo": {"threatTypes": THREAT_TYPES, "platformTypes": ["ANY_PLATFORM"],
                           "threatEntryTypes": ["URL"], "threatEntries": [{"url": u} for u in urls]}}
    data = json.dumps(body).encode("utf-8")
    wait = 5
    for _ in range(5):
        req = urllib.request.Request(endpoint + "?key=" + key, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                print("  返答 {}。{} 秒待ってやり直します".format(e.code, wait))
                time.sleep(wait)
                wait *= 2
                continue
            sys.exit("中止：返答 {}：{}".format(e.code, e.read().decode("utf-8", "replace")[:300].replace(key, "（キー）")))
        except Exception as e:
            print("  接続できませんでした（{}）。{} 秒待ってやり直します".format(str(e).replace(key, "（キー）"), wait))
            time.sleep(wait)
            wait *= 2
    sys.exit("中止：何度やり直しても問い合わせられませんでした")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out", help="html_probe.py の結果のフォルダ（既定 out）")
    ap.add_argument("--key-file", default="~/.gsb_key", help="GSB のキーのファイル（既定 ~/.gsb_key）")
    ap.add_argument("--dry-run", action="store_true", help="問い合わせずに、送る件数だけ表示する")
    ap.add_argument("--endpoint", default=ENDPOINT, help=argparse.SUPPRESS)   # テスト用
    a = ap.parse_args()

    out = os.path.expanduser(a.out)
    links = read_tsv(os.path.join(out, "links.tsv"))
    if not links:
        sys.exit("{} がありません。先に html_probe.py を実行してください".format(os.path.join(a.out, "links.tsv")))
    urls = sorted({r["gsb_url"] for r in links if r["gsb_url"]})
    print("送る URL：{} 件（問い合わせ {} 回）".format(len(urls), (len(urls) + BATCH - 1) // BATCH))
    if a.dry_run:
        return
    key = read_key(a.key_file)
    if not key:
        sys.exit("GSB のキーがありません。環境変数 GSB_API_KEY か、ファイル {} に入れてください".format(a.key_file))

    t = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hits = {}
    for i in range(0, len(urls), BATCH):
        res = ask(key, urls[i:i + BATCH], a.endpoint)
        for m in res.get("matches", []):
            u = m.get("threat", {}).get("url", "")
            hits.setdefault(u, set()).add(m.get("threatType", ""))
        time.sleep(0.2)

    p = os.path.join(out, "gsb_results.tsv")
    with open(p + ".part", "w", encoding="utf-8") as w:
        w.write("gsb_url\tthreat\tchecked_at\n")
        for u in urls:
            w.write("{}\t{}\t{}\n".format(u, ",".join(sorted(hits.get(u, []))), t))
    os.replace(p + ".part", p)

    bad = {r["source"]: set() for r in links}
    for r in links:
        if r["gsb_url"] in hits:
            bad[r["source"]].add(r["gsb_url"])
    print("悪性と判定：{} 件（AI要約 {} 件、上位10件 {} 件）　問い合わせ日時 {}".format(
        len(hits), len(bad.get("aio", ())), len(bad.get("organic", ())), t))
    for u in sorted(hits):
        print("  ！ {}  {}".format(u.replace("http", "hxxp", 1).replace(".", "[.]"), ",".join(sorted(hits[u]))))
    print("一覧：{}".format(os.path.abspath(make_report(out))))


if __name__ == "__main__":
    main()
