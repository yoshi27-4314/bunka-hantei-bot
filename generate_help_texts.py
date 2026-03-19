"""
HTMLマニュアルからヘルプ用テキストを自動生成するスクリプト

使い方:
    python generate_help_texts.py

HTMLファイル（正）からMarkdown形式のテキストファイルを自動生成する。
生成されたテキストはSlackヘルプ機能でAIに渡すために使用される。

外部ライブラリ不要（標準ライブラリのみ）。
beautifulsoup4 がインストールされていれば自動的にそちらを使用する。
"""

import os
import re
from html.parser import HTMLParser

# ============================================================
# マッピング（HTMLファイル → 出力テキストファイル）
# 新しいマニュアルを追加するときは、ここに1行追加するだけ
# ============================================================
MANUAL_MAP = {
    "docs/分荷判定_作業マニュアル.html": "docs/help/分荷判定.txt",
    "docs/商品撮影_作業マニュアル.html": "docs/help/商品撮影.txt",
    "docs/出品保管_作業マニュアル.html": "docs/help/出品保管.txt",
    "docs/梱包出荷_作業マニュアル.html": "docs/help/梱包出荷.txt",
}

# プロジェクトルート
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


# ============================================================
# HTML → 構造化ノードツリー（標準ライブラリのみ）
# ============================================================

class Node:
    """シンプルなDOMノード"""
    def __init__(self, tag="", attrs=None):
        self.tag = tag
        self.attrs = dict(attrs) if attrs else {}
        self.children = []
        self.text = ""  # テキストノード用

    @property
    def classes(self):
        return self.attrs.get("class", "").split()

    def find_all(self, tag):
        """指定タグの子孫を全て返す"""
        result = []
        for child in self.children:
            if isinstance(child, Node) and child.tag == tag:
                result.append(child)
            if isinstance(child, Node):
                result.extend(child.find_all(tag))
        return result

    def find_by_class(self, cls):
        """指定クラスを持つ子孫を全て返す"""
        result = []
        for child in self.children:
            if isinstance(child, Node):
                if cls in child.classes:
                    result.append(child)
                result.extend(child.find_by_class(cls))
        return result

    def get_text(self):
        """全テキストを連結して返す"""
        parts = []
        for child in self.children:
            if isinstance(child, str):
                parts.append(child)
            elif isinstance(child, Node):
                if child.tag == "br":
                    parts.append("\n")
                else:
                    parts.append(child.get_text())
        return "".join(parts)

    def get_text_clean(self):
        """テキストを取得して余分な空白を除去"""
        text = self.get_text()
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()


class HTMLTreeBuilder(HTMLParser):
    """HTMLをNodeツリーに変換するパーサー"""

    VOID_ELEMENTS = {
        "br", "hr", "img", "input", "meta", "link",
        "area", "base", "col", "embed", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__()
        self.root = Node(tag="root")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag=tag, attrs=attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID_ELEMENTS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        # スタックを巻き戻す（閉じタグが欠けている場合の安全策）
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                return

    def handle_data(self, data):
        if data.strip() or data:
            self.stack[-1].children.append(data)

    def handle_entityref(self, name):
        from html import unescape
        self.stack[-1].children.append(unescape(f"&{name};"))

    def handle_charref(self, name):
        from html import unescape
        self.stack[-1].children.append(unescape(f"&#{name};"))


def parse_html(html_content):
    """HTMLをNodeツリーにパースする"""
    builder = HTMLTreeBuilder()
    builder.feed(html_content)
    return builder.root


# ============================================================
# ノードツリー → Markdownテキスト変換
# ============================================================

def node_to_markdown(node):
    """ノードからMarkdownテキストを抽出"""
    if isinstance(node, str):
        return node

    tag = node.tag
    classes = node.classes

    # code タグはバッククォートで囲む
    if tag == "code":
        return f"`{node.get_text()}`"

    # br タグは改行
    if tag == "br":
        return "\n"

    # テーブルの処理
    if tag == "table":
        return convert_table_node(node)

    # code-block クラスの div はコードブロックとして保持
    if "code-block" in classes:
        text = node.get_text().strip()
        return f"\n```\n{text}\n```\n"

    # folder-tree クラスの div もコードブロック
    if "folder-tree" in classes:
        text = node.get_text().strip()
        return f"\n```\n{text}\n```\n"

    # リスト処理
    if tag in ("ul", "ol"):
        return convert_list_node(node)

    # 子要素を再帰的に処理
    parts = []
    for child in node.children:
        parts.append(node_to_markdown(child))
    return "".join(parts)


def convert_table_node(table_node):
    """テーブルノードをMarkdownテーブルに変換"""
    rows = []
    for tr in table_node.find_all("tr"):
        cells = []
        for cell_tag in ("th", "td"):
            for cell in [c for c in tr.children if isinstance(c, Node) and c.tag == cell_tag]:
                text = cell.get_text_clean()
                text = re.sub(r"\s+", " ", text)
                cells.append(text)
        if cells:
            rows.append(cells)

    if not rows:
        return ""

    max_cols = max(len(row) for row in rows)
    for row in rows:
        while len(row) < max_cols:
            row.append("")

    lines = []
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("| " + " | ".join(["---"] * max_cols) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")

    return "\n" + "\n".join(lines) + "\n"


def convert_list_node(list_node):
    """リストノードをMarkdownリストに変換"""
    items = []
    for child in list_node.children:
        if isinstance(child, Node) and child.tag == "li":
            text = child.get_text_clean()
            text = re.sub(r"\s+", " ", text)
            items.append(f"- {text}")
    if not items:
        return ""
    return "\n" + "\n".join(items) + "\n"


def html_to_help_text(html_content):
    """HTMLコンテンツをヘルプ用テキストに変換"""
    root = parse_html(html_content)

    # style タグの中身を除去するため、styleノードを除去
    def remove_nodes(parent, tag_name):
        parent.children = [
            c for c in parent.children
            if not (isinstance(c, Node) and c.tag == tag_name)
        ]
        for child in parent.children:
            if isinstance(child, Node):
                remove_nodes(child, tag_name)

    remove_nodes(root, "style")

    # page div を探す
    page_divs = root.find_by_class("page")
    page_div = page_divs[0] if page_divs else root

    # 表紙（cover）を除去
    page_div.children = [
        c for c in page_div.children
        if not (isinstance(c, Node) and "cover" in c.classes)
    ]

    output_parts = []

    for element in page_div.children:
        if isinstance(element, str):
            text = element.strip()
            if text:
                output_parts.append(text)
            continue

        if not isinstance(element, Node):
            continue

        tag = element.tag
        classes = element.classes

        # h1 → # セクション名
        if tag == "h1":
            text = element.get_text_clean()
            if text:
                output_parts.append(f"\n# {text}\n")
            continue

        # h2 → ## 小見出し名
        if tag == "h2":
            text = element.get_text_clean()
            if text:
                output_parts.append(f"\n## {text}\n")
            continue

        # h3 → ### 項目名
        if tag == "h3":
            text = element.get_text_clean()
            if text:
                output_parts.append(f"\n### {text}\n")
            continue

        # page-break は区切り線
        if "page-break" in classes:
            output_parts.append("\n---\n")
            continue

        # flow（フロー図）の処理
        if "flow" in classes or "redo-flow" in classes:
            steps = []
            for cls_name in ("flow-step", "redo-step"):
                for step in element.find_by_class(cls_name):
                    steps.append(step.get_text_clean())
            if steps:
                output_parts.append("\n" + " → ".join(steps) + "\n")
            continue

        # その他の要素は中身を抽出
        text = node_to_markdown(element).strip()
        if text:
            output_parts.append(f"\n{text}\n")

    # 結合して整形
    result = "\n".join(output_parts)

    # 連続する空行を2行に制限
    result = re.sub(r"\n{4,}", "\n\n\n", result)
    # 先頭・末尾の空行を除去
    result = result.strip() + "\n"

    return result


def main():
    # docs/help/ フォルダがなければ作成
    help_dir = os.path.join(PROJECT_ROOT, "docs", "help")
    os.makedirs(help_dir, exist_ok=True)

    success_count = 0
    error_count = 0

    for html_path, txt_path in MANUAL_MAP.items():
        abs_html = os.path.join(PROJECT_ROOT, html_path)
        abs_txt = os.path.join(PROJECT_ROOT, txt_path)

        if not os.path.exists(abs_html):
            print(f"[SKIP] {html_path} が見つかりません")
            error_count += 1
            continue

        print(f"[変換中] {html_path} → {txt_path}")

        with open(abs_html, "r", encoding="utf-8") as f:
            html_content = f.read()

        help_text = html_to_help_text(html_content)

        with open(abs_txt, "w", encoding="utf-8") as f:
            f.write(help_text)

        # 見出し数と本文量を確認
        h1_count = help_text.count("\n# ")
        h2_count = help_text.count("\n## ")
        h3_count = help_text.count("\n### ")
        line_count = len(help_text.splitlines())
        char_count = len(help_text)

        print(f"  → 完了: h1={h1_count}, h2={h2_count}, h3={h3_count}, "
              f"{line_count}行, {char_count}文字")
        success_count += 1

    print(f"\n=== 変換完了: 成功 {success_count} / 失敗 {error_count} ===")


if __name__ == "__main__":
    main()
