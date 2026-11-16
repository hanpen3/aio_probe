#!/usr/bin/env python3
"""
html_probe.py：ブラウザで保存した Google の検索結果ページ（HTML）から、
AI要約（AI による概要）の部分と、その中のリンク、通常の検索結果の上位10件を取り出して比べる。

■ 使い方
  python3 html_probe.py                      # html/ の中の .html を全部処理して、out/ に保存する
  python3 html_probe.py --html-dir 別のフォルダ --out 別のフォルダ
  GSB で調べるのは gsb_check.py（このあとに実行する）

■ AI要約の範囲の決め方（WD データの分析で決めた規則と同じ）
  ページの文字とリンクを上から順に並べ、空白の違いを無視して「行全体が一致」するもので判定する
  1. 対象：「関連する質問」の行より前にある、最初の見出し（「AI による概要」「AI Overview」）から始まるブロック1つ
     （「関連する質問」の中の AI の回答は対象外）
  2. 始まり：その見出し。終わりまでの間に別の見出しがあれば、最後の見出しを始まりにする
  3. 終わり：始まりの後に最初に出てくる「もっと見る」「ウェブ検索結果」、または末尾の注意書き
  4. 判定
       aio        AI要約あり（リンクを取り出す）
       none       「関連する質問」より前に見出しが無い（AI要約なし）
       unclosed   終わりが見つからない
       component  見出しの直後がエラーの文言・「検索中」・「生成中」（中身の無い部品だけ）
       too_long   始まりと終わりの間が --max-items 行（既定 300）を超える（範囲が不確か）
■ AI要約のリンク
  範囲の中の、外部のサイトへのリンク（http / https で、ホスト名が google.〜 でないもの）。
  # 以降を除いた URL が同じものは1回だけ（出てきた順）
■ 通常の検索結果の上位 --top 件（既定 10）
  最初の「ウェブ検索結果」より後ろで、次の形のリンクを、出てきた順に取る
    リンクの後ろ数行以内に、そのリンクのサイトの住所（https://ホスト名）がそのまま表示されている
  （検索結果の1件は「リンク → ページ名 → サイト名 → https://ホスト名 › …」の形で表示されるため。
    関連する質問・動画・画像などの枠のリンクは、この形にならないので入らない）
  AI要約の範囲の中のリンクは入れない。# 以降を除いた URL が同じものは1回だけ
■ 比べ方（AI要約のリンクのうち、上位10件と共通のもの）
  URL    ：スキーム・クエリ・# を除き、ホスト名を小文字にし、先頭の www. と末尾の / を除いたものが同じ
  ドメイン：ホスト名（小文字、先頭の www. を除く）が同じ

■ 保存するもの（--out のフォルダ。実行するたびに、html/ の中身から全部作り直す）
  searches.tsv    ページごとの要点
  links.tsv       リンクごと（AI要約か上位10件か、順位、URL、ドメイン、共通か、GSB に送る形）
  aio_text.jsonl  AI要約の範囲の文章（ページごとに1行）
  report.html     一覧（ブラウザで開く。URL は押せない文字として表示。gsb_check.py の後なら GSB の結果も入る）

■ 安全のため：リンク先のサイトには一切アクセスしない。report.html の URL は押せない文字として表示する
"""
import argparse
import datetime
import glob
import html
import html.parser
import json
import os
import re
import sys
import urllib.parse

HEADS = ["AI による概要", "AI Overview"]
ERRORS = ["この検索では AI による概要を表示できません",
          "現在、AI による概要を生成できません。また後でお試しください。",
          "An AI Overview is not available for this search",
          "Can't generate an AI overview right now. Try again later."]
LOADING = ["検索中", "生成中"]
ENDS = {"M": "もっと見る",
        "W": "ウェブ検索結果",
        "E1": "生成 AI は試験運用中です。",
        "E2": "AI の回答には間違いが含まれている場合があります。",
        "E3": "AI は不正確な情報を表示することがあるため、生成された回答を再確認するようにしてください",
        "E4": "AI responses may include mistakes."}
PAA = "関連する質問"
WEB = "ウェブ検索結果"
LINK = "  -> "
GOOGLE_HOST = re.compile(r"(^|\.)google\.[a-z.]+$")

SEARCH_HEAD = ["id", "query", "aio", "status", "end", "items", "aio_links", "aio_domains", "organic",
               "organic_domains", "common_url", "common_domain", "gen", "file"]
LINK_HEAD = ["id", "query", "source", "rank", "url", "gsb_url", "domain", "in_top10_url", "in_top10_domain",
             "in_aio_url", "in_aio_domain", "text"]


def squash(s):
    return re.sub(r"\s+", "", s)


HEAD_SET = {squash(t) for t in HEADS}
COMPONENT_SET = {squash(t) for t in ERRORS + LOADING}
END_OF = {squash(t): k for k, t in ENDS.items()}


class PageText(html.parser.HTMLParser):
    """表示される文字とリンクを出てくる順に集める（script / style / noscript / svg の中は除く）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.items, self.title, self.in_title = 0, [], "", False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        elif tag == "a" and not self.skip and dict(attrs).get("href"):
            self.items.append(LINK + dict(attrs)["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title += data
            return
        t = " ".join(data.split())
        if t and not self.skip:
            self.items.append(t)


# ---------------------------------------------------------------- URL の扱い
def split(u):
    try:
        p = urllib.parse.urlsplit((u or "").strip())
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    return p


def external(u):
    """外部のサイトへのリンクなら、その URL を返す（Google 自身のリンクや相対パスは None）。"""
    p = split(u)
    if p is None or GOOGLE_HOST.search(p.hostname.lower()):
        return None
    return u.strip()


def domain_of(u):
    p = split(u)
    if p is None:
        return None
    h = p.hostname.lower()
    return h[4:] if h.startswith("www.") else h


def url_key(u):
    p = split(u)
    return None if p is None else domain_of(u) + p.path.rstrip("/")


def gsb_form(u):
    """GSB に送る形：スキーム＋ホスト名＋パス（クエリと # は送らない）。"""
    p = split(u)
    return None if p is None else urllib.parse.urlunsplit((p.scheme.lower(), p.netloc, p.path or "/", "", ""))


def no_frag(u):
    return u.split("#", 1)[0]


def defang(u):
    return u.replace("http", "hxxp", 1).replace(".", "[.]")


# ---------------------------------------------------------------- 取り出し
def find_block(lines, max_items):
    """AI要約の範囲を探す。(status, 始まり, 終わり, 終わりの種類)"""
    text = [None if t.startswith(LINK) else squash(t) for t in lines]
    paa = next((i for i, t in enumerate(text) if t == squash(PAA)), len(lines))
    first = next((i for i, t in enumerate(text[:paa]) if t in HEAD_SET), None)
    if first is None:
        return "none", None, None, ""
    start, end, kind = first, None, ""
    for i in range(first + 1, len(lines)):
        t = text[i]
        if t is None:
            continue
        if t in HEAD_SET:
            start = i
        elif t in END_OF:
            end, kind = i, END_OF[t]
            break
    if end is None:
        return "unclosed", start, None, ""
    nxt = next((t for t in text[start + 1:end] if t is not None), None)
    if nxt in COMPONENT_SET:
        return "component", start, end, kind
    if end - start - 1 > max_items:
        return "too_long", start, end, kind
    return "aio", start, end, kind


def next_text(lines, i, limit):
    """i 番目の後ろの、リンクでない行を最大 limit 個返す。"""
    out = []
    for t in lines[i + 1:]:
        if t.startswith(LINK):
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


def aio_links(lines, s, e):
    seen = {}
    for i in range(s + 1, e):
        if not lines[i].startswith(LINK):
            continue
        u = external(lines[i][len(LINK):])
        if u is None:
            continue
        k = no_frag(u)
        if k not in seen:
            nt = next_text(lines, i, 1)
            seen[k] = {"url": k, "text": nt[0] if nt else ""}
    return list(seen.values())


def organic_links(lines, s, e, top):
    """最初の「ウェブ検索結果」より後ろの、検索結果の形のリンクを top 件。"""
    web = next((i for i, t in enumerate(lines) if not t.startswith(LINK) and squash(t) == squash(WEB)), None)
    if web is None:
        return []
    seen = {}
    for i in range(web + 1, len(lines)):
        if s is not None and s <= i <= (e if e is not None else s):
            continue
        if not lines[i].startswith(LINK):
            continue
        u = external(lines[i][len(LINK):])
        if u is None:
            continue
        p = split(u)
        origin = "{}://{}".format(p.scheme.lower(), p.netloc.lower())
        after = next_text(lines, i, 6)
        if not any(t.lower().rstrip("/") == origin for t in after):
            continue
        k = no_frag(u)
        if k not in seen:
            seen[k] = {"url": k, "text": after[0] if after else ""}
            if len(seen) >= top:
                break
    return list(seen.values())


def query_of(title, path):
    t = re.sub(r"\s*-\s*Google\s*(検索|Search)\s*$", "", (title or "").strip())
    return t or os.path.splitext(os.path.basename(path))[0]


# ---------------------------------------------------------------- 一覧（report.html）
def read_tsv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        head = f.readline().rstrip("\n").split("\t")
        return [dict(zip(head, l.rstrip("\n").split("\t"))) for l in f if l.strip()]


def make_report(out):
    """searches.tsv・links.tsv・aio_text.jsonl と、あれば gsb_results.tsv から report.html を作る。"""
    searches = read_tsv(os.path.join(out, "searches.tsv"))
    links = read_tsv(os.path.join(out, "links.tsv"))
    gsb = {r["gsb_url"]: r for r in read_tsv(os.path.join(out, "gsb_results.tsv"))}
    checked = sorted({r["checked_at"] for r in gsb.values()})
    texts = {}
    p = os.path.join(out, "aio_text.jsonl")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for l in f:
                d = json.loads(l)
                texts[d["id"]] = d["text"]
    by = {}
    for r in links:
        by.setdefault(r["id"], []).append(r)

    def threat(r):
        g = gsb.get(r["gsb_url"])
        return "" if g is None else (g["threat"] or "-")

    e = html.escape
    o = ["<!doctype html><html lang='ja'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'><title>AI要約の調査結果</title>",
         "<style>body{font-family:sans-serif;margin:16px;max-width:1200px}table{border-collapse:collapse;width:100%;"
         "font-size:13px;margin-bottom:8px}td,th{border:1px solid #ccc;padding:3px 6px;vertical-align:top;"
         "word-break:break-all}th{background:#eee}.bad{background:#fdd;font-weight:bold}.com{background:#eef6ff}"
         "details{margin:8px 0 16px}code{font-size:12px}.muted{color:#777}</style></head><body>",
         "<h1>AI要約の調査結果</h1>",
         "<p>URL は安全のため押せない文字として表示しています。赤い行は GSB で悪性と判定されたもの（URL を崩して表示）。"
         "青い行は AI要約と上位10件で共通の URL。</p>",
         "<p class='muted'>GSB：{}</p>".format(e("問い合わせ日時 " + "、".join(checked)) if checked
                                              else "まだ調べていません（gsb_check.py を実行すると入ります）")]
    o.append("<table><tr><th>ID</th><th>検索語</th><th>AI要約</th><th>判定</th><th>AI要約のリンク（ドメイン）</th>"
             "<th>上位10件（ドメイン）</th><th>共通 URL</th><th>共通ドメイン</th><th>GSB 悪性（AI要約）</th>"
             "<th>GSB 悪性（上位10件）</th></tr>")
    for s in searches:
        rs = by.get(s["id"], [])
        bad_a = len({r["gsb_url"] for r in rs if r["source"] == "aio" and threat(r) not in ("", "-")})
        bad_o = len({r["gsb_url"] for r in rs if r["source"] == "organic" and threat(r) not in ("", "-")})
        g = "" if not gsb else None
        o.append("<tr{}><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}（{}）</td><td>{}（{}）</td><td>{}</td>"
                 "<td>{}</td><td>{}</td><td>{}</td></tr>".format(
                     " class='bad'" if bad_a or bad_o else "", e(s["id"]), e(s["query"]), e(s["aio"]), e(s["status"]),
                     e(s["aio_links"]), e(s["aio_domains"]), e(s["organic"]), e(s["organic_domains"]),
                     e(s["common_url"]), e(s["common_domain"]),
                     "" if g == "" else bad_a, "" if g == "" else bad_o))
    o.append("</table>")
    for s in searches:
        o.append("<details><summary>[{}] {}（AI要約：{}）</summary>".format(e(s["id"]), e(s["query"]), e(s["aio"])))
        if texts.get(s["id"]):
            o.append("<h4>AI要約の範囲の文章</h4><pre style='white-space:pre-wrap'>{}</pre>".format(
                e("\n".join(texts[s["id"]]))))
        o.append("<table><tr><th>どこ</th><th>順位</th><th>URL（押せない）</th><th>ドメイン</th>"
                 "<th>相手側にもある（URL/ドメイン）</th><th>GSB</th><th>リンクの文字</th></tr>")
        for r in by.get(s["id"], []):
            t = threat(r)
            bad = t not in ("", "-")
            if r["source"] == "aio":
                both = (r["in_top10_url"], r["in_top10_domain"])
            else:
                both = (r["in_aio_url"], r["in_aio_domain"])
            cls = " class='bad'" if bad else (" class='com'" if both[0] == "1" else "")
            o.append("<tr{}><td>{}</td><td>{}</td><td><code>{}</code></td><td>{}</td><td>{}/{}</td><td>{}</td>"
                     "<td>{}</td></tr>".format(cls, "AI要約" if r["source"] == "aio" else "上位10件", e(r["rank"]),
                                               e(defang(r["url"]) if bad else r["url"]),
                                               e(defang(r["domain"]) if bad else r["domain"]),
                                               e(both[0]), e(both[1]), e(t), e(r["text"])))
        o.append("</table></details>")
    o.append("</body></html>")
    p = os.path.join(out, "report.html")
    with open(p + ".part", "w", encoding="utf-8") as w:
        w.write("\n".join(o))
    os.replace(p + ".part", p)
    return p


# ---------------------------------------------------------------- 本体
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--html-dir", default="html", help="保存した HTML を置いたフォルダ（既定 html）")
    ap.add_argument("--out", default="out", help="結果の保存先のフォルダ（既定 out）")
    ap.add_argument("--max-items", type=int, default=300, help="AI要約の範囲の上限の行数（既定 300）")
    ap.add_argument("--top", type=int, default=10, help="通常の検索結果を上から何件取るか（既定 10）")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(os.path.expanduser(a.html_dir), "*.htm*")))
    if not files:
        sys.exit("{} に .html のファイルがありません".format(a.html_dir))
    out = os.path.expanduser(a.out)
    os.makedirs(out, exist_ok=True)

    srows, lrows, texts = [], [], []
    for path in files:
        sid = os.path.splitext(os.path.basename(path))[0]
        with open(path, "rb") as f:
            raw = f.read()
        p = PageText()
        try:
            p.feed(raw.decode("utf-8", "replace"))
        except Exception:
            pass
        lines = p.items
        query = query_of(p.title, path)
        status, s, e, kind = find_block(lines, a.max_items)
        aio = aio_links(lines, s, e) if status == "aio" else []
        org = organic_links(lines, s, e if status != "unclosed" else None, a.top)
        gen = int(any(squash(t) == "生成中" for t in lines if not t.startswith(LINK)))

        org_u = {url_key(x["url"]) for x in org}
        org_d = {domain_of(x["url"]) for x in org}
        aio_u = {url_key(x["url"]) for x in aio}
        aio_d = {domain_of(x["url"]) for x in aio}
        srows.append([sid, query, "あり" if status == "aio" else "なし", status, kind,
                      (e - s - 1) if s is not None and e is not None else "",
                      len(aio), len(aio_d), len(org), len(org_d), len(aio_u & org_u), len(aio_d & org_d), gen,
                      os.path.basename(path)])
        for n, x in enumerate(aio, 1):
            lrows.append([sid, query, "aio", n, x["url"], gsb_form(x["url"]), domain_of(x["url"]),
                          int(url_key(x["url"]) in org_u), int(domain_of(x["url"]) in org_d), "", "", x["text"]])
        for n, x in enumerate(org, 1):
            lrows.append([sid, query, "organic", n, x["url"], gsb_form(x["url"]), domain_of(x["url"]), "", "",
                          int(url_key(x["url"]) in aio_u), int(domain_of(x["url"]) in aio_d), x["text"]])
        if status == "aio":
            texts.append({"id": sid, "query": query, "text": [t for t in lines[s:e + 1] if not t.startswith(LINK)]})
        print("[{}] 検索語：{}　AI要約：{}（{}）　AI要約のリンク {} 件　上位{}件 {} 件　共通 URL {} 件・ドメイン {} 件".format(
            sid, query, "あり" if status == "aio" else "なし", status, len(aio), a.top, len(org),
            len(aio_u & org_u), len(aio_d & org_d)))

    def write(name, head, rows):
        p = os.path.join(out, name)
        with open(p + ".part", "w", encoding="utf-8") as w:
            w.write("\t".join(head) + "\n")
            for r in rows:
                w.write("\t".join(str(v).replace("\t", " ").replace("\n", " ") for v in r) + "\n")
        os.replace(p + ".part", p)
    write("searches.tsv", SEARCH_HEAD, srows)
    write("links.tsv", LINK_HEAD, lrows)
    with open(os.path.join(out, "aio_text.jsonl"), "w", encoding="utf-8") as w:
        for t in texts:
            w.write(json.dumps(t, ensure_ascii=False) + "\n")
    rep = make_report(out)
    print("\n{} ページを処理しました。一覧：{}".format(len(files), os.path.abspath(rep)))
    print("GSB で調べるには：python3 gsb_check.py --out {}".format(a.out))


if __name__ == "__main__":
    main()
