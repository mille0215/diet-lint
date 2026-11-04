#!/usr/bin/env python3
"""[DIEt]…[/DIEt] タグを含む Nashorn 向け JS を整形・lint する（Python のみ、exe 不要）。

使い方:
  python diet_tool.py format <ファイル...>   整形して上書き
  python diet_tool.py lint   <ファイル...>   lint結果を表示（行番号は元ファイルと一致）
  python diet_tool.py check  <ファイル...>   整形が必要か確認のみ（CI向け、変更しない）
  python diet_tool.py save   <ファイル...>   整形してから lint（保存時タスク用）

必要なパッケージ: pip install -r requirements.txt （jsbeautifier, esprima）
設定: 同じフォルダの diet.json（整形オプション、追加グローバル、ルールの有効/無効）

仕組み:
  [DIEt]文字列[/DIEt] を __DIET__(番号); というダミー呼び出しに置換してから処理し（mask）、
  結果を元のタグに戻す（unmask）。文として扱われるので、波括弧なしの if/for の本体でも壊れない。
  lint 時はタグの中身が複数行でも同じ行数になるようコメントで改行を補う。
"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "diet.json"
# ファイルごとの解析結果（トップレベルの宣言、lint の結果）のキャッシュ。
# ファイルの更新日時とサイズが変わったものだけ解析し直す
DECLS_CACHE = ROOT / ".diet_cache.json"
# キャッシュの形式を変えたら上げる（古いキャッシュは捨てる）
CACHE_VERSION = 3
# ツールのコード（このファイルと diet_lint.py）のハッシュ。コードが変われば lint の結果のキャッシュは使わない
TOOL_HASH = hashlib.sha1(
    b"".join(p.read_bytes() for p in (Path(__file__).resolve(), ROOT / "diet_lint.py"))
).hexdigest()

try:
    import jsbeautifier

    from diet_lint import (
        Linter,
        add_semicolons,
        dupe_global_message,
        syntax_error_index,
        tokenize,
        top_level_decls,
    )
except ImportError as e:
    sys.exit(
        f"必要なパッケージがありません（{e.name}）。"
        "VSCode のタスク「DIEt: セットアップ（パッケージ導入）」を実行するか、"
        f".venv の python で pip install -r {ROOT / 'requirements.txt'} を実行してください。"
        f"（実行中の python: {sys.executable}）"
    )

# タグ本体と、直後に同じ行で続く ";"（あれば）をまとめて捕まえる
#   group(1): タグの中身 / group(2): タグ直後の ";"（前の空白を含む）
TAG = re.compile(r"\[DIEt\](.*?)\[/DIEt\]([ \t]*;)?", re.DOTALL)
# 復元用：整形でスペースや改行が入っても拾えるようにゆるく書く。
#   semi: insert_semicolons が補った ";" を取り除くために捕まえる
#   pre: 直前の空白・改行。元ファイルで同じ行の前にコードがあった場合は整形で入った改行を元に戻し、
#        行頭（インデントなし）にあった場合は整形で付いたインデントを取り除く
#   id: タグの番号（blocks の添字）。lint 用の改行保持コメント /*…*/ があっても読み飛ばす
PLACEHOLDER = re.compile(r"(?P<semi>;?)(?P<pre>\s*)__DIET__\(\s*(?P<id>\d+)\s*(?:/\*[\s\S]*?\*/)?\s*\);?")
# プレースホルダ直後の空白・改行（元ファイルで同じ行の後ろにコードがあった場合に、元の空白へ置き換える）
GAP_AFTER = re.compile(r"[ \t]*(?:\r?\n[ \t]*)?")
# 構文エラー位置がプレースホルダの先頭かどうかの判定用
PLACEHOLDER_START = re.compile(r"__DIET__\((\d+)")

# diet.json の options.tagIndent に指定できる値
TAG_INDENT_MODES = ("never", "keep", "format")

# コマンドライン引数で指定できるモード
MODES = ("format", "lint", "check", "save")


def load_config() -> dict:
    """diet.json を読み込む。

    Returns:
        設定の辞書。diet.json がなければ空の辞書（すべて既定値で動く）。
    """
    if CONFIG.exists():
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    return {}


def mask(src: str, keep_lines: bool) -> tuple[str, list[dict]]:
    """[DIEt] タグを ``__DIET__(番号);`` に置き換える。

    タグの中身や前後の空白は、復元（unmask）に使うため blocks に退避する。
    中身は JS として解析しないので、引用符や ``*/`` を含んでいても壊れない。

    blocks の各要素は次のキーを持つ辞書:
        text: 復元するタグの文字列（タグ直後の ";" は含まない）。
        semicolon: タグ直後の同じ行に ";" があったか。
        inserted: insert_semicolons がタグの前に ";" を補ったか。
        pre_sep: 同じ行でタグの前にコードがあれば、その間の空白。なければ None。
        post_sep: 同じ行でタグの後ろにコードがあれば、その間の空白。なければ None。
        indent: 行頭のタグなら元のインデント（"" ならインデントなし）。前にコードがあれば None。

    Args:
        src: 元のソースコード。
        keep_lines: True なら中身の改行数を、改行だけを含むコメント ``/*…*/`` で保持する（lint 用。
            行番号を元ファイルと一致させるため）。整形時は jsbeautifier がコメント内の改行を
            崩すので False にする。

    Returns:
        置換後のソースコードと、退避したタグ情報のリスト（添字がプレースホルダの番号）。
    """
    blocks = []

    def rep(m: re.Match) -> str:
        """1 つのタグをプレースホルダに置き換え、情報を blocks に追加する。"""
        i = len(blocks)
        # 同じ行でタグの前後にあるテキスト（別のタグも含む）
        before = src[src.rfind("\n", 0, m.start()) + 1 : m.start()]
        line_end = src.find("\n", m.end())
        after = src[m.end() : len(src) if line_end < 0 else line_end].rstrip("\r")
        blocks.append(
            {
                "text": m.group(0).rstrip(" \t;") if m.group(2) else m.group(0),
                "semicolon": bool(m.group(2)),
                "inserted": False,
                # 前にコードがあれば、コードとタグの間の空白（before の末尾の空白）
                "pre_sep": before[len(before.rstrip()) :] if before.strip() else None,
                # 後ろにコードがあれば、タグとコードの間の空白（after の先頭の空白）
                "post_sep": after[: len(after) - len(after.lstrip())] if after.strip() else None,
                "indent": before if not before.strip() else None,
            }
        )
        newlines = m.group(1).count("\n")
        pad = "/*" + "\n" * newlines + "*/" if keep_lines and newlines else ""
        return f"__DIET__({i}{pad});"

    return insert_semicolons(TAG.sub(rep, src), blocks), blocks


def needs_semicolon(tokens: list, i: int) -> bool:
    """プレースホルダ ``__DIET__`` のトークン（tokens[i]）の前に ";" が必要かを判定する。

    直前のトークンが式の終わり（識別子・リテラル・``)``・``]``・後置の ``++`` など）なら、
    ``$(x)__DIET__(0);`` のように式とつながって構文エラーになるので ";" が必要。
    ただし ``if (x)`` / ``for (…)`` / ``while (…)`` / ``with (…)`` の ``)`` の後ろは文の本体なので不要
    （補うと本体が空文になってしまう）。改行をはさむ場合は自動セミコロン挿入が働くので不要。

    Args:
        tokens: tokenize の結果。
        i: プレースホルダのトークンの位置。

    Returns:
        ";" を補う必要があれば True。
    """
    if i == 0:
        return False
    prev = tokens[i - 1]
    if prev.type in ("Identifier", "String", "Numeric", "Boolean", "Null", "RegularExpression", "Template"):
        return True
    if prev.type == "Keyword":
        return prev.value == "this"
    if prev.type != "Punctuator":
        return False
    if prev.value in ("]", "++", "--"):
        return True
    if prev.value != ")":
        return False
    # 対応する "(" を探し、その前が制御構文のキーワードなら不要
    depth = 0
    for j in range(i - 1, -1, -1):
        t = tokens[j]
        if t.type == "Punctuator" and t.value == ")":
            depth += 1
        elif t.type == "Punctuator" and t.value == "(":
            depth -= 1
            if depth == 0:
                before = tokens[j - 1] if j > 0 else None
                return not (before and before.type == "Keyword" and before.value in ("if", "for", "while", "with"))
    return False


def insert_semicolons(code: str, blocks: list[dict]) -> str:
    """同じ行のコードとタグの間に ";" がない書き方（``$(x)[DIEt]…``）に ";" を補う。

    置換後は ``$(x)__DIET__(0);`` となり構文エラーになるので、プレースホルダの前に ";" を補う。
    まず 1 回のトークン分割で、同じ行の直前のトークンから補う場所をまとめて決める（needs_semicolon）。
    それでも構文エラーが残れば、構文エラーの位置がプレースホルダの先頭で、かつ同じ行の前に
    コードがある場合だけ ";" を補って再解析する（エラーがなくなるまで、最大でタグの数だけ繰り返す）。
    どのタグにも一律に補うと、``if (x) [DIEt]…`` の本体が空文になってしまうため、この方式にしている。

    Args:
        code: mask でタグを置換したソースコード。
        blocks: mask が作ったタグ情報。";" を補ったタグは ``inserted`` を True にする（復元時に取り除く）。

    Returns:
        ";" を補ったソースコード。タグと関係ない構文エラーはそのまま残す（lint が parse エラーとして報告する）。
    """
    if not blocks:
        return code
    tokens = tokenize(code) or []
    positions = []
    for i, t in enumerate(tokens):
        if t.type != "Identifier" or t.value != "__DIET__" or i == 0:
            continue
        start = t.range[0]
        # 直前のトークンとの間に改行がある（自動セミコロン挿入が働く）なら不要
        if "\n" in code[tokens[i - 1].range[1] : start]:
            continue
        m = PLACEHOLDER_START.match(code, start)
        if m and needs_semicolon(tokens, i):
            positions.append((start, int(m.group(1))))
    for start, idx in reversed(positions):  # 後ろから補えば前の位置がずれない
        blocks[idx]["inserted"] = True
        code = code[:start] + ";" + code[start:]

    # 判定しきれなかった場合の予備: 構文エラーの位置で 1 つずつ補う
    for _ in range(len(blocks)):
        idx = syntax_error_index(code)
        if idx is None:
            break
        m = PLACEHOLDER_START.match(code, idx)
        line_start = code.rfind("\n", 0, idx) + 1
        if not m or not code[line_start:idx].strip():
            break  # タグとは関係ない構文エラー
        blocks[int(m.group(1))]["inserted"] = True
        code = code[:idx] + ";" + code[idx:]
    return code


def unmask(src: str, blocks: list[dict], tag_indent: str = "never") -> str:
    """整形後のコードのプレースホルダを元のタグに戻す。

    プレースホルダを前から 1 つずつ処理し、整形（jsbeautifier）で変わった前後の空白を、
    mask で記録した元ファイルの状態に合わせて直す。

    - 元ファイルで同じ行の前にコードがあった: 整形で入った改行を、元の空白（pre_sep）に戻す
    - 元ファイルで行頭にあった: tag_indent に従ってインデントを決める
    - 元ファイルで同じ行の後ろにコードがあった: 整形で入った改行を、元の空白（post_sep）に戻す

    Args:
        src: 整形後のソースコード。
        blocks: mask が作ったタグ情報。
        tag_indent: 行頭にあるタグのインデント（diet.json の options.tagIndent）。
            "never": インデントしない（常に行頭）。
            "keep": 元ファイルのインデントのまま。
            "format": 整形（jsbeautifier）のインデントに合わせる。

    Returns:
        タグを復元したソースコード。

    Raises:
        RuntimeError: 復元できなかったプレースホルダが残った場合（呼び出し側はファイルを書かない）。
    """
    parts, pos = [], 0
    while m := PLACEHOLDER.search(src, pos):
        b = blocks[int(m.group("id"))]
        parts.append(src[pos : m.start()])
        # insert_semicolons が補った ";" は元ファイルになかったので落とす
        semi = "" if b["inserted"] else m.group("semi")
        pre = m.group("pre")
        if b["pre_sep"] is not None:
            pre = b["pre_sep"]
        elif b["indent"] is not None and tag_indent != "format":
            indent = "" if tag_indent == "never" else b["indent"]
            # 整形後も行頭にある場合だけ、整形で付いたインデント（最後の改行より後）を置き換える
            # （整形で前の行のコードとつながった場合はそのまま）
            if "\n" in pre:
                pre = pre[: pre.rfind("\n") + 1] + indent
            elif m.start() == 0:
                pre = indent
        parts.append(semi + pre + b["text"] + (";" if b["semicolon"] else ""))
        pos = m.end()
        if b["post_sep"] is not None:
            # 直後の空白・改行（1 行分まで）を読み飛ばし、元の空白に置き換える
            parts.append(b["post_sep"])
            pos = GAP_AFTER.match(src, pos).end()
    parts.append(src[pos:])

    out = "".join(parts)
    if "__DIET__" in out:
        raise RuntimeError("プレースホルダの復元に失敗しました（ファイルは変更していません）")
    return out


def beautify(code: str, fmt: dict) -> str:
    """コードを整形する（jsbeautifier を使う）。

    Args:
        code: 整形するソースコード（タグはプレースホルダに置換済みのもの）。
        fmt: diet.json の "format"（jsbeautifier のオプション名と値）。

    Returns:
        整形後のソースコード。
    """
    opts = jsbeautifier.default_options()
    for k, v in fmt.items():
        setattr(opts, k, v)
    out = jsbeautifier.beautify(code, opts)
    # jsbeautifier は for each を "for each(" と出力するので "for each (" に揃える
    return re.sub(r"\bfor each\(", "for each (", out)


def print_results(path: Path, results: list[dict]) -> None:
    """Linter の結果を GitHub Actions 形式で標準出力に書く。

    VSCode の tasks.json の problemMatcher がこの形式を拾って「問題」パネルに表示する。

    Args:
        path: lint したファイル（絶対パスにして出力する）。
        results: Linter.lint の結果。
    """
    for r in results:
        print(
            f"::{r['level']} title={r['rule']},file={path.resolve()},line={r['line']},endLine={r['endLine']},"
            f"col={r['col']},endColumn={r['endCol']}::{r['message']}"
        )


def format_file(path: Path, original: str, mode: str, config: dict, tag_indent: str) -> tuple[str, bool]:
    """1 ファイルを整形する（format / check / save モード）。

    構文エラーがあれば整形しない（jsbeautifier は壊れたコードも整形してしまうため）。

    Args:
        path: 対象ファイル。
        original: ファイルの現在の内容。
        mode: "format"（上書き）/ "check"（確認のみ）/ "save"（上書きして続けて lint）。
        config: diet.json の内容。
        tag_indent: diet.json の options.tagIndent。

    Returns:
        整形後の内容（check モードや整形しなかった場合は original のまま）と、
        失敗扱い（終了コード 1）にすべきかどうか。
    """
    masked, blocks = mask(original, keep_lines=False)
    if any(r["rule"] == "parse" for r in Linter({}).lint(masked)):
        if mode == "save":
            return original, False  # 続く lint が構文エラーの位置を報告する
        sys.stderr.write(f"{path}: 構文エラーがあるため整形しませんでした（lint で位置を確認してください）\n")
        return original, True

    if config.get("rules", {}).get("semi", "off") != "off":
        masked = add_semicolons(masked)  # 文末の ";" が抜けている文に補う
    formatted = unmask(beautify(masked, config.get("format", {})), blocks, tag_indent)
    if formatted == original:
        return original, False
    if mode == "check":
        print(f"{path}: 整形が必要です")
        return original, True
    # 改行コードは整形結果のまま書く（newline="" で \n を変換しない）
    path.write_text(formatted, encoding="utf-8", newline="")
    if mode == "format":
        print(f"{path}: 整形しました")
    return formatted, False


class Cache:
    """ファイルごとの解析結果のキャッシュ（ツールのフォルダの .diet_cache.json）。

    ファイルの絶対パスごとに、更新日時・サイズ（stamp）と、トップレベルの宣言（decls）、
    lint の結果（lint: 判定に使った条件のハッシュと結果）を持つ。stamp が変わったファイルは解析し直す。

    保存時タスクは保存のたびにフォルダ内の全ファイルを lint するので、変わっていないファイルは
    キャッシュの結果を使って速くする。
    """

    def __init__(self):
        """キャッシュを読む。ない・壊れている・形式が古い場合は空から始める。"""
        try:
            data = json.loads(DECLS_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.entries: dict = data.get("files", {}) if data.get("version") == CACHE_VERSION else {}
        self.changed = False

    def entry(self, p: Path) -> dict:
        """ファイルのキャッシュを返す。ファイルが変わっていれば空にする。

        Args:
            p: ファイル（絶対パス）。

        Returns:
            そのファイルのキャッシュの辞書（"decls" / "lint" を入れる）。

        Raises:
            OSError: ファイルの情報が読めない場合。
        """
        st = p.stat()
        stamp = [st.st_mtime_ns, st.st_size]
        e = self.entries.get(str(p))
        if not e or e["stamp"] != stamp:
            e = self.entries[str(p)] = {"stamp": stamp}
            self.changed = True
        return e

    def save(self) -> None:
        """キャッシュを書く（変わっていなければ何もしない）。

        保存時タスクが途中で止められても壊れないよう、一時ファイルに書いてから置き換える。
        書けなくても lint は続ける（次回また解析するだけ）。消えたファイルの分は捨てる。
        """
        if not self.changed:
            return
        files = {k: v for k, v in self.entries.items() if Path(k).exists()}
        tmp = DECLS_CACHE.with_name(f"{DECLS_CACHE.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({"version": CACHE_VERSION, "files": files}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, DECLS_CACHE)
        except OSError:
            tmp.unlink(missing_ok=True)


def folder_decls(folder: Path, cache: Cache) -> dict[Path, dict | None]:
    """フォルダ内の .js ファイルごとに、トップレベルの var / function 宣言を調べる。

    Args:
        folder: 対象のフォルダ。
        cache: 解析結果のキャッシュ。

    Returns:
        ファイル（絶対パス、名前順）から top_level_decls の結果への辞書。構文エラーのファイルは None。
        読めないファイルは含めない。
    """
    result: dict[Path, dict | None] = {}
    for p in sorted(folder.resolve().glob("*.js")):
        try:
            e = cache.entry(p)
            if "decls" not in e:
                # 行番号を元ファイルと一致させるため、改行数を保持して置換する
                e["decls"] = top_level_decls(mask(p.read_text(encoding="utf-8"), keep_lines=True)[0])
            result[p] = e["decls"]
        except (OSError, UnicodeDecodeError):
            continue
    return result


def sibling_decls(me: Path, decls: dict[Path, dict | None], config: dict) -> dict[str, list[dict]]:
    """自分以外のファイルのトップレベル宣言を、名前ごとにまとめる。

    本番では dtm.require などで同じグローバルスコープに読み込まれるため、別ファイルで定義した
    関数・変数を宣言なしで使える。lint ではこれを globals として扱い、2 重定義の検出にも使う。
    options.siblingGlobals が false なら空にする。

    Args:
        me: lint するファイル（絶対パス。これ自身は除く）。
        decls: folder_decls の結果。
        config: diet.json の内容。

    Returns:
        名前から ``[{"file": ファイル名, "kind": 種類, "line": 行, "loop": ループ変数だけか}, …]`` への辞書
        （ファイル名順）。
    """
    if not config.get("options", {}).get("siblingGlobals", True):
        return {}
    result: dict[str, list[dict]] = {}
    for p, d in decls.items():
        if p == me:
            continue
        for name, info in (d or {}).items():  # 構文エラーのファイルは d が None
            result.setdefault(name, []).append({"file": p.name, **info})
    return result


def lint_file(path: Path, config: dict, cache: Cache, decls: dict[Path, dict | None]) -> list[dict]:
    """1 ファイルを lint する（出力は呼び出し側で print_results を使う）。

    ファイルが変わっておらず、判定の条件（別ファイルの宣言・設定・ツールのコード）も同じなら、
    キャッシュの結果をそのまま返す。

    Args:
        path: 対象ファイル。
        config: diet.json の内容。
        cache: 解析結果のキャッシュ。
        decls: 対象ファイルのフォルダの folder_decls の結果。

    Returns:
        Linter.lint と同じ形の結果。
    """
    me = path.resolve()
    siblings = sibling_decls(me, decls, config)
    # 別ファイルの宣言の行は条件に含めない（行がずれるたびに全ファイルを lint し直さないため）。
    # 行は no-dupe-global のメッセージにしか使わないので、出力の前に作り直す
    names = {name: [{k: v for k, v in o.items() if k != "line"} for o in os_] for name, os_ in siblings.items()}
    condition = json.dumps(
        [names, config.get("globals"), config.get("rules"), config.get("options"), TOOL_HASH],
        sort_keys=True,
        ensure_ascii=False,
    )
    key = hashlib.sha1(condition.encode("utf-8")).hexdigest()
    e = cache.entry(me)
    if e.get("lint", {}).get("key") == key:
        results = e["lint"]["results"]
    else:
        # 改行数を保持して置換するので、結果の行番号は元ファイルと一致する
        source = me.read_text(encoding="utf-8")
        results = Linter(config, siblings).lint(mask(source, keep_lines=True)[0])
        e["lint"] = {"key": key, "results": results}
        cache.changed = True
    for r in results:
        if r["rule"] == "no-dupe-global":
            r["message"] = dupe_global_message(r["kind"], r["name"], r["files"], siblings)
    return results


def lint_targets(files: list[Path], config: dict) -> list[Path]:
    """対象のファイル（lint するもの）を決める。

    options.lintScope が "folder"（既定）なら、指定したファイルと同じフォルダの .js をすべて対象にする。
    VSCode のタスクは実行のたびに前回の「問題」を消すので、保存したファイルだけを lint すると
    他のファイルの結果が消えてしまうため。"file" なら指定したファイルだけ。

    Args:
        files: コマンドラインで指定したファイル。
        config: diet.json の内容。

    Returns:
        lint するファイル（重複なし）。
    """
    if config.get("options", {}).get("lintScope", "folder") == "file":
        return list(dict.fromkeys(files))
    targets: dict[Path, None] = {}
    for f in files:
        targets[f.resolve()] = None
        for p in sorted(f.resolve().parent.glob("*.js")):
            targets[p] = None
    return list(targets)


def main() -> None:
    """コマンドライン引数を解釈して、各ファイルを整形・lint する。

    終了コードは、整形できなかった・整形が必要（check）・error レベルの問題があった
    のいずれかがあれば 1、なければ 0。
    """
    if len(sys.argv) < 3 or sys.argv[1] not in MODES:
        sys.exit(__doc__)
    mode, files = sys.argv[1], sys.argv[2:]
    config = load_config()
    tag_indent = config.get("options", {}).get("tagIndent", "never")
    if tag_indent not in TAG_INDENT_MODES:
        choices = " / ".join(TAG_INDENT_MODES)
        sys.exit(f"diet.json の options.tagIndent は {choices} のいずれかにしてください（現在: {tag_indent}）")
    failed = False
    paths = [Path(f) for f in files]
    rewritten_at = None  # 整形でファイルを書き換えた時刻（書き換えなければ None）

    if mode in ("format", "check", "save"):
        for path in paths:
            original = path.read_text(encoding="utf-8")
            formatted, ng = format_file(path, original, mode, config, tag_indent)
            failed |= ng
            if formatted != original:
                rewritten_at = time.monotonic()

    if mode in ("lint", "save"):
        cache = Cache()
        decls_by_folder: dict[Path, dict] = {}
        all_results = []
        for path in lint_targets(paths, config):
            folder = path.resolve().parent
            if folder not in decls_by_folder:
                decls_by_folder[folder] = folder_decls(folder, cache)
            results = lint_file(path, config, cache, decls_by_folder[folder])
            all_results.append((path, results))
            failed |= any(r["level"] == "error" for r in results)
        cache.save()
        if mode == "save" and rewritten_at is not None:
            wait_for_editor_reload(rewritten_at, config)
        for path, results in all_results:
            print_results(path, results)

    sys.exit(1 if failed else 0)


def wait_for_editor_reload(rewritten_at: float, config: dict) -> None:
    """整形でファイルを書き換えたあと、エディタが読み込み直すまで lint 結果の出力を待つ。

    VSCode は、開いているファイルがディスク上で書き換わると読み込み直し、そのとき変わった行にある
    「問題」の位置を編集に合わせて動かす。読み込み直す前に新しい内容に対する lint 結果を出すと、
    正しい位置の波線がそこで動かされてずれてしまうため、書き換えから一定時間たってから出力する。
    待ち時間は options.reloadWaitMs（既定 800。0 で待たない）。lint にかかった時間は差し引く。

    Args:
        rewritten_at: ファイルを書き換えた時刻（time.monotonic の値）。
        config: diet.json の内容。
    """
    wait = config.get("options", {}).get("reloadWaitMs", 800) / 1000
    remaining = wait - (time.monotonic() - rewritten_at)
    if remaining > 0:
        time.sleep(remaining)


if __name__ == "__main__":
    main()
