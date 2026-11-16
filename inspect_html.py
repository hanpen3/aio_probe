#!/usr/bin/env python3
"""
保存した Google の検索結果ページ（HTML）の中身を、上から順に表示して確かめる。
WD のデータで使ったのと同じ方法（タグの外の文字と、リンクの href を出てくる順に。script / style / noscript / svg の中は除く）。

■ AI要約に関係しそうな行には、行番号の横に印を付ける
    H  見出し（AI による概要 / AI Overview）       X  エラーの文言
    E  末尾の注意書き（E1〜E4）                     M  もっと見る
    W  ウェブ検索結果                               Q  関連する質問
    R  AI モードの回答:                             G  Google の転送用のリンク（/goto、/url）
■ 最後に、印ごとの数と、リンクの種類ごとの数をまとめて出す

使い方:
  python3 inspect_html.py html/influenza_shojo.html | less              # 全部
  python3 inspect_html.py html/influenza_shojo.html --around H | less   # 最初の見出しの前後だけ（前20行・後200行）
"""
import argparse
import collections
import html.parser
import re
import sys
from urllib.parse import urlsplit

MARK = {
    "AIによる概要": "H", "AIOverview": "H",
    "この検索ではAIによる概要を表示できません": "X", "現在、AIによる概要を生成できません。また後でお試しください。": "X",
    "AnAIOverviewisnotavailableforthissearch": "X", "Can'tgenerateanAIoverviewrightnow.Tryagainlater.": "X",
    "生成AIは試験運用中です。": "E1", "AIの回答には間違いが含まれている場合があります。": "E2",
    "AIは不正確な情報を表示することがあるため、生成された回答を再確認するようにしてください": "E3",
    "AIresponsesmayincludemistakes.": "E4",
    "もっと見る": "M", "ウェブ検索結果": "W", "関連する質問": "Q", "AIモードの回答:": "R",
}


class PageText(html.parser.HTMLParser):
    """表示される文字とリンクを出てくる順に集める（WD の分析で使った view_noq.py と同じもの）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip, self.items = 0, []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        elif tag == "a" and not self.skip and dict(attrs).get("href"):
            self.items.append("  -> " + dict(attrs)["href"])

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        t = " ".join(data.split())
        if t and not self.skip:
            self.items.append(t)


def mark_of(t):
    if t.startswith("  -> "):
        u = t[5:]
        p = urlsplit(u)
        if re.search(r"(^|\.)google\.[a-z.]+$", (p.hostname or "").lower()) and p.path in ("/goto", "/url") \
                or u.startswith(("/goto", "/url")):
            return "G"
        return ""
    return MARK.get(re.sub(r"\s+", "", t), "")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("html", help="保存した HTML ファイル")
    ap.add_argument("--around", default="", help="この印の最初の行の前後だけ表示する（例：H）")
    ap.add_argument("--before", type=int, default=20)
    ap.add_argument("--after", type=int, default=200)
    a = ap.parse_args()

    with open(a.html, "rb") as f:
        raw = f.read()
    p = PageText()
    p.feed(raw.decode("utf-8", "replace"))
    lines = p.items
    marks = [mark_of(t) for t in lines]

    lo, hi = 0, len(lines)
    if a.around:
        pos = next((i for i, m in enumerate(marks) if m == a.around), None)
        if pos is None:
            print("印 {} の行が見つかりませんでした".format(a.around))
        else:
            lo, hi = max(0, pos - a.before), min(len(lines), pos + a.after + 1)
    for i in range(lo, hi):
        print("{:5d} {:2s} {}".format(i + 1, marks[i], lines[i]))

    print("\n== まとめ（ページ全体）==")
    print("中身の行の数：{}".format(len(lines)))
    c = collections.Counter(m for m in marks if m)
    for k in ["H", "X", "E1", "E2", "E3", "E4", "R", "M", "W", "Q", "G"]:
        pos = [str(i + 1) for i, m in enumerate(marks) if m == k]
        print("  {:2s} {:4d} 行  {}".format(k, c[k], ("行番号 " + ", ".join(pos[:15]) + (" …" if len(pos) > 15 else "")) if pos else ""))
    kinds = collections.Counter()
    for t in lines:
        if t.startswith("  -> "):
            u = t[5:]
            if mark_of(t) == "G":
                kinds["Google の転送用（/goto、/url）"] += 1
            elif u.startswith(("http://", "https://")):
                h = (urlsplit(u).hostname or "").lower()
                kinds["Google の他のページ" if re.search(r"(^|\.)google\.[a-z.]+$", h) else "外部のサイト（直接）"] += 1
            else:
                kinds["相対パス・# など"] += 1
    print("リンクの種類：")
    for k, n in kinds.most_common():
        print("  {:4d}  {}".format(n, k))


if __name__ == "__main__":
    main()
