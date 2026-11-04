"""Nashorn(ES5.1) 向けの簡易リンター。

esprima で JavaScript を AST に変換し、スコープ解析をしながら木をたどってルール違反を集める。
Nashorn 拡張の ``for each (x in y)`` は、前処理で通常の for-in に置き換えてから解析する。

ルール（diet.json の ``"rules"`` で ``"error"`` / ``"warning"`` / ``"off"`` を指定）:
    parse: 構文エラー（常に error、オフ不可）
    no-es6: Nashorn(ES5.1) で使えない ES2015 以降の構文
    no-undef: 宣言されていない変数の参照・代入（暗黙のグローバル）
    no-unused-vars: 宣言したが使っていない変数・関数（既定では関数内のみ）
    eqeqeq: ``==`` / ``!=`` の使用
    no-dupe-keys: オブジェクトリテラルのキー重複
    no-unreachable: return / throw / break / continue の後の到達不能コード
    no-debugger: debugger 文
    no-redeclare: 同じファイル・同じスコープでの 2 重定義（既定ではループ変数同士は除く）
    no-dupe-global: 同じフォルダの別ファイルとのトップレベルの 2 重定義（既定ではループ変数同士は除く）
    no-shadow: 関数の中で、外側（外側の関数・グローバル・別ファイルのグローバル）と同じ名前を宣言している
    semi: 文末の ";" がない（diet_tool.py の整形で自動的に補う）

Examples:
    >>> results = Linter({"rules": {"no-undef": "error"}}).lint("x = 1;")
"""

import re
from collections.abc import Iterator

import esprima
from esprima import nodes
from esprima.error_handler import Error as EsprimaError

# ES5.1 の組み込み＋ Nashorn の組み込み（プロジェクト固有のものは diet.json の globals に書く）
# fmt: off
BUILTINS = {
    # ES5.1
    "Object", "Function", "Array", "String", "Boolean", "Number", "Math", "Date", "RegExp", "JSON",
    "Error", "EvalError", "RangeError", "ReferenceError", "SyntaxError", "TypeError", "URIError",
    "parseInt", "parseFloat", "isNaN", "isFinite", "eval", "undefined", "NaN", "Infinity",
    "encodeURI", "encodeURIComponent", "decodeURI", "decodeURIComponent", "escape", "unescape",
    # Nashorn
    "Java", "JavaImporter", "JavaAdapter", "Packages", "java", "javax", "com", "org", "net", "edu",
    "print", "echo", "load", "loadWithNewGlobal", "exit", "quit", "readLine", "readFully",
    "__FILE__", "__DIR__", "__LINE__",
    "arguments",  # トップレベルではスクリプト引数
    "importPackage", "importClass",  # nashorn:mozilla_compat.js
    "$ARG", "$ENV", "$EXEC", "$OUT", "$ERR", "$EXIT", "$OPTIONS",  # -scripting モード
    # [DIEt] プレースホルダ（diet_tool.py がタグを __DIET__(番号); に置き換える）
    "__DIET__",
}
# fmt: on

# Nashorn 拡張の for each (x in y)。esprima は解析できないので、"each" を同じ長さの空白に置き換えて
# 通常の for-in として解析する（行・列の位置は変わらない）
FOR_EACH = re.compile(r"\bfor(\s+)each(\s*)\(")

# 関数を表すノードの種類（新しいスコープを作る）
FUNCTIONS = ("FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression")

# ES2015 以降でだけ現れるノードの種類と、メッセージに使う構文名
ES6_NODES = {
    "ArrowFunctionExpression": "アロー関数",
    "TemplateLiteral": "テンプレートリテラル",
    "TaggedTemplateExpression": "タグ付きテンプレート",
    "ClassDeclaration": "class",
    "ClassExpression": "class",
    "ForOfStatement": "for...of",
    "SpreadElement": "スプレッド構文(...)",
    "RestElement": "残余引数(...)",
    "ObjectPattern": "分割代入",
    "ArrayPattern": "分割代入",
    "AssignmentPattern": "デフォルト引数",
    "YieldExpression": "yield",
    "AwaitExpression": "await",
    "Super": "super",
    "MetaProperty": "new.target",
}

# 文末に ";" が必要な文の種類（semi ルール）
SEMI_STATEMENTS = (
    "ExpressionStatement",
    "VariableDeclaration",
    "ReturnStatement",
    "ThrowStatement",
    "BreakStatement",
    "ContinueStatement",
    "DoWhileStatement",
    "DebuggerStatement",
)

# この文の後ろは到達できない、という文の種類
TERMINATORS = ("ReturnStatement", "ThrowStatement", "BreakStatement", "ContinueStatement")


def strip_nashorn_ext(source: str) -> str:
    """Nashorn 独自構文を、esprima が解析できる標準の構文に置き換える。

    現在は ``for each (`` を ``for      (`` （同じ文字数）にするだけ。文字数を変えないので、
    解析結果の行・列は元のソースとそのまま一致する。

    Args:
        source: JavaScript のソースコード。

    Returns:
        置き換え後のソースコード。
    """
    return FOR_EACH.sub(lambda m: "for" + m.group(1) + "    " + m.group(2) + "(", source)


def syntax_error_index(source: str) -> int | None:
    """構文エラーの位置を調べる。

    Args:
        source: JavaScript のソースコード。

    Returns:
        最初の構文エラーの位置（ソース先頭からの文字オフセット）。構文エラーがなければ None。
    """
    try:
        esprima.parseScript(strip_nashorn_ext(source))
    except EsprimaError as err:
        return getattr(err, "index", None)
    return None


def tokenize(source: str) -> list | None:
    """ソースコードをトークンに分ける（位置 range 付き）。

    Args:
        source: JavaScript のソースコード。

    Returns:
        esprima のトークンのリスト（各トークンは type / value / range を持つ）。字句エラーなら None。
    """
    try:
        return esprima.tokenize(strip_nashorn_ext(source), {"range": True})
    except EsprimaError:
        return None


def top_level_decls(source: str) -> dict[str, dict] | None:
    """トップレベル（グローバルスコープ）で宣言されている var / function を調べる。

    同じフォルダの別ファイルの定義を globals として使うため、また 2 重定義を調べるために使う。

    Args:
        source: JavaScript のソースコード（[DIEt] タグはプレースホルダに置換済みのもの）。

    Returns:
        名前から ``{"kind": "var" か "function", "line": 宣言の行, "loop": ループ変数だけか}`` への辞書。
        同じ名前の宣言が複数あれば、function を優先し、ループ変数以外の最初の宣言の行を使う。
        構文エラーで解析できなければ None。
    """
    try:
        tree = esprima.parseScript(strip_nashorn_ext(source), {"loc": True})
    except EsprimaError:
        return None
    top = Scope(None, is_function=True, is_global=True)
    Linter({}).hoist(tree.body, top)
    result = {}
    for name, decls in top.hoisted.items():
        main = next((d for d in decls if d[1] == "function"), None) or next((d for d in decls if not d[2]), decls[0])
        result[name] = {"kind": main[1], "line": main[0].loc.start.line, "loop": all(d[2] for d in decls)}
    return result


def is_placeholder(stmt: nodes.Node) -> bool:
    """[DIEt] タグを置き換えたプレースホルダの文（``__DIET__(番号);``）かを返す。

    Args:
        stmt: 文のノード。

    Returns:
        プレースホルダの文なら True。
    """
    if stmt.type != "ExpressionStatement" or stmt.expression.type != "CallExpression":
        return False
    callee = stmt.expression.callee
    return callee.type == "Identifier" and callee.name == "__DIET__"


def missing_semicolons(tree: nodes.Node, source: str) -> list[nodes.Node]:
    """文末に ";" が必要なのに書かれていない文を集める（semi ルールと、整形での自動修正に使う）。

    対象は SEMI_STATEMENTS の文。esprima の range は文末の ";" を含むので、range の最後の文字が
    ";" でなければ書かれていない（自動セミコロン挿入に頼っている）。
    ``for (var i = 0; …)`` / ``for (var x in …)`` の var は文ではないので除く。
    プレースホルダの文は mask が ";" を付けるので、ここには出てこない。

    Args:
        tree: ``{"range": True}`` を付けて解析した AST。
        source: 解析したソースコード。

    Returns:
        ";" が書かれていない文のノード（ソース上の順）。
    """
    found = []
    stack: list = [tree]
    while stack:
        n = stack.pop()
        if not isinstance(n, nodes.Node):
            continue
        if n.type in SEMI_STATEMENTS and source[n.range[1] - 1] != ";" and not is_placeholder(n):
            found.append(n)
        if n.type == "ForStatement" and n.init is not None and n.init.type == "VariableDeclaration":
            # for の初期化部の var は文ではない（中の関数式などはたどる）
            stack.extend(c for c in children(n) if c is not n.init)
            stack.extend(children(n.init))
            continue
        if n.type in ("ForInStatement", "ForOfStatement") and n.left.type == "VariableDeclaration":
            stack.extend(c for c in children(n) if c is not n.left)
            stack.extend(children(n.left))
            continue
        stack.extend(children(n))
    return sorted(found, key=lambda n: n.range[0])


def add_semicolons(source: str) -> str:
    """文末の ";" が書かれていない文に ";" を補う（整形での自動修正）。

    Args:
        source: JavaScript のソースコード（[DIEt] タグはプレースホルダに置換済みのもの）。

    Returns:
        ";" を補ったソースコード。構文エラーなら何もせずに返す。
    """
    try:
        tree = esprima.parseScript(strip_nashorn_ext(source), {"range": True})
    except EsprimaError:
        return source
    # 文の終わりの位置が後ろのものから補えば、まだ補っていない位置がずれない
    # （開始位置の順だと、関数式の中の return と外側の var のように入れ子の文で位置がずれる）
    for end in sorted({n.range[1] for n in missing_semicolons(tree, source)}, reverse=True):
        source = source[:end] + ";" + source[end:]
    return source


def dupe_global_message(kind: str, name: str, files: list[str], siblings: dict[str, list[dict]]) -> str:
    """no-dupe-global のメッセージを作る（相手のファイル名と、そこで定義している行を並べる）。

    diet_tool.py は lint の結果をキャッシュするが、相手のファイルの行がずれただけなら
    lint し直さず、この関数でメッセージだけを作り直す。

    Args:
        kind: 自分のファイルでの宣言の種類（"var" / "function"）。
        name: 名前。
        files: 同じ名前を定義している相手のファイル名。
        siblings: 別ファイルのトップレベル宣言（Linter.siblings と同じ形）。

    Returns:
        メッセージ。
    """
    lines = {o["file"]: o["line"] for o in siblings.get(name, [])}
    what = "関数" if kind == "function" else "変数"
    places = "、".join(f"{f}（{lines.get(f, '?')} 行目）" for f in files)
    return f"{what} {name} は {places} でも定義されています"


def node_pos(node: nodes.Node) -> tuple[int, int]:
    """ノードの開始位置を (行, 列) で返す（ソース上の順に並べるため）。

    Args:
        node: AST ノード。

    Returns:
        開始位置の (行, 列)。
    """
    return (node.loc.start.line, node.loc.start.column)


def children(node: nodes.Node) -> Iterator[nodes.Node]:
    """AST ノードの直下の子ノードを順に返す。

    esprima のノードは辞書のように属性をたどれるので、値がノード（またはノードのリスト）の
    属性をすべて子とみなす。位置情報（loc / range）は除く。

    Args:
        node: 親ノード。

    Yields:
        子ノード。
    """
    for key, value in node.items():
        if key in ("loc", "range"):
            continue
        if isinstance(value, nodes.Node):
            yield value
        elif isinstance(value, list):
            for v in value:
                if isinstance(v, nodes.Node):
                    yield v


class Scope:
    """変数のスコープ（関数スコープ、catch 節、with 文の本体）。

    宣言された名前ごとに ``{"node": 宣言した識別子, "kind": 種類, "used": 参照されたか}`` を持つ。
    kind は ``"var"`` / ``"function"`` / ``"param"`` / ``"fname"`` （名前付き関数式の名前）/
    ``"catch"`` / ``"builtin"`` のいずれか。

    Attributes:
        parent: 外側のスコープ。グローバルスコープなら None。
        is_function: 関数スコープ（var の巻き上げ先）かどうか。
        is_global: グローバルスコープかどうか。
        is_with: with 文の本体かどうか（名前がオブジェクトのプロパティの可能性がある）。
        decls: 名前から宣言情報への辞書。
        hoisted: 巻き上げた var / function 宣言の、名前から ``[(識別子, 種類, ループ変数か), …]`` への辞書
            （同じ名前の宣言をソース上の順にすべて残す。no-redeclare / no-dupe-global に使う）。
    """

    def __init__(self, parent: "Scope | None", is_function: bool, is_global: bool = False, is_with: bool = False):
        """スコープを作る。

        Args:
            parent: 外側のスコープ。グローバルスコープなら None。
            is_function: 関数スコープかどうか。
            is_global: グローバルスコープかどうか。
            is_with: with 文の本体かどうか。
        """
        self.parent = parent
        self.is_function = is_function
        self.is_global = is_global
        self.is_with = is_with
        self.decls: dict[str, dict] = {}
        self.hoisted: dict[str, list[tuple[nodes.Node, str, bool]]] = {}

    def declare(self, ident: nodes.Node | None, kind: str) -> None:
        """名前を宣言する。

        同じ名前が既に宣言されていれば最初の宣言を残す（``var x; var x;`` や、
        var と function の同名宣言のとき）。

        Args:
            ident: 宣言する識別子ノード。Identifier 以外（None を含む）は無視する。
            kind: 宣言の種類（"var" / "function" / "param" など）。
        """
        if ident is None or ident.type != "Identifier":
            return
        self.decls.setdefault(ident.name, {"node": ident, "kind": kind, "used": False})

    def lookup(self, name: str) -> dict | None:
        """名前を内側のスコープから順に探す。

        Args:
            name: 探す名前。

        Returns:
            見つかった宣言情報（``decls`` の値）。どのスコープにもなければ None。
        """
        s = self
        while s:
            if name in s.decls:
                return s.decls[name]
            s = s.parent
        return None

    def in_with(self) -> bool:
        """このスコープが with 文の本体の中にあるかを返す。

        Returns:
            自分か外側のいずれかのスコープが with 文の本体なら True。
        """
        s = self
        while s:
            if s.is_with:
                return True
            s = s.parent
        return False


def pattern_ids(pat: nodes.Node | None) -> list[nodes.Node]:
    """宣言のパターン（引数や var の左辺）から、宣言される識別子をすべて取り出す。

    ES5.1 では単なる識別子だけだが、ES6 の分割代入・デフォルト引数・残余引数にも対応しておく
    （no-es6 で報告したうえで、no-undef / no-unused-vars の誤検出を防ぐため）。

    Args:
        pat: パターンのノード。None なら空リストを返す。

    Returns:
        Identifier ノードのリスト。
    """
    if pat is None:
        return []
    t = pat.type
    if t == "Identifier":
        return [pat]
    if t == "ObjectPattern":
        # {a, b: c, ...rest} の各プロパティの値側（Property 以外は RestElement）
        return [i for p in pat.properties for i in pattern_ids(p.value if p.type == "Property" else p)]
    if t == "ArrayPattern":
        # [a, , b] の穴（None）は飛ばす
        return [i for e in pat.elements if e for i in pattern_ids(e)]
    if t == "AssignmentPattern":
        return pattern_ids(pat.left)
    if t == "RestElement":
        return pattern_ids(pat.argument)
    return []


class Linter:
    """AST をたどってルール違反を集めるリンター。

    1 つのインスタンスで ``lint`` を呼ぶのは 1 回だけを想定している（結果を蓄積するため）。

    Attributes:
        rules: ルール名からレベル（"error" / "warning" / "off"）への辞書。書かれていないルールは off。
        options: diet.json の options（unusedCheckTopLevel、eqeqeqAllowNull など）。
        globals: 宣言なしで使ってよい名前（組み込み＋ diet.json の globals）。
        siblings: 同じフォルダの別ファイルのトップレベル宣言。名前から
            ``[{"file": ファイル名, "kind": 種類, "line": 行}, …]`` への辞書。
            宣言なしで使ってよい名前として扱い、no-dupe-global の判定にも使う。
        results: 見つかった問題のリスト。
    """

    def __init__(self, config: dict, siblings: dict[str, list[dict]] | None = None):
        """設定を読み込んでリンターを作る。

        Args:
            config: diet.json の内容（"rules" / "options" / "globals" を参照する）。
            siblings: 同じフォルダの別ファイルのトップレベル宣言（diet_tool.py が集める）。
        """
        self.rules = config.get("rules", {})
        self.options = config.get("options", {})
        self.globals = BUILTINS | set(config.get("globals", []))
        self.siblings = siblings or {}
        self.results: list[dict] = []

    # ---- 報告 -------------------------------------------------------------
    def report(self, rule: str, node: nodes.Node, message: str, at_end: bool = False) -> bool:
        """問題を 1 件記録する。ルールが off なら何もしない。

        Args:
            rule: ルール名。
            node: 問題の位置を示すノード（loc の範囲を使う）。
            message: 表示するメッセージ。
            at_end: True ならノード全体ではなく、最後の 1 文字の位置にする（semi で文末を示すため）。

        Returns:
            記録したら True（ルールが off なら False）。
        """
        level = self.rules.get(rule, "off")
        if level == "off":
            return False
        s, e = node.loc.start, node.loc.end
        # esprima の列は 0 始まりなので、エディタ向けに 1 始まりにする
        line, col = (e.line, max(e.column, 1)) if at_end else (s.line, s.column + 1)
        self.results.append(
            {
                "level": level,
                "rule": rule,
                "message": message,
                "line": line,
                "col": col,
                "endLine": e.line,
                "endCol": e.column + 1,
            }
        )
        return True

    # ---- 入口 -------------------------------------------------------------
    def lint(self, source: str) -> list[dict]:
        """ソースコードを lint する。

        構文エラーがあれば parse エラーを 1 件だけ返す（それ以外のルールは調べない）。

        Args:
            source: JavaScript のソースコード（[DIEt] タグはプレースホルダに置換済みのもの）。

        Returns:
            問題のリスト（行・列の順）。各要素は level / rule / message / line / col /
            endLine / endCol を持つ辞書（行・列は 1 始まり）。
        """
        try:
            tree = esprima.parseScript(strip_nashorn_ext(source), {"loc": True, "range": True})
        except EsprimaError as err:
            line = getattr(err, "lineNumber", 1) or 1
            col = getattr(err, "column", 1) or 1
            msg = str(err).split(": ", 1)[-1]  # 先頭の "Line N: " を取り除く
            self.results.append(
                {
                    "level": "error",
                    "rule": "parse",
                    "message": f"構文エラー: {msg}",
                    "line": line,
                    "col": col,
                    "endLine": line,
                    "endCol": col + 1,
                }
            )
            return self.results

        # グローバルスコープ: 先に巻き上げてから走査する（宣言より前の参照を no-undef にしないため）
        top = Scope(None, is_function=True, is_global=True)
        self.hoist(tree.body, top)
        self.check_redeclare(top)
        self.check_dupe_global(top)
        for stmt in tree.body:
            self.walk(stmt, top)
        self.check_unreachable(tree.body)
        self.finish_scope(top)
        for n in missing_semicolons(tree, source):
            self.report("semi", n, "文末に ; がありません（保存時の整形で自動的に補われます）", at_end=True)
        self.results.sort(key=lambda r: (r["line"], r["col"]))
        return self.results

    # ---- スコープ ---------------------------------------------------------
    def hoist(self, body: list[nodes.Node] | nodes.Node, scope: Scope) -> None:
        """変数宣言（var）と関数宣言（function）を関数スコープに巻き上げる。

        ブロックや if / for などの中もたどるが、ネストした関数（とクラス）の中には入らない。

        Args:
            body: 関数本体（またはプログラム）の文のリスト、または 1 つのノード。
            scope: 巻き上げ先の関数スコープ。
        """
        stack = list(body) if isinstance(body, list) else [body]
        found: list[tuple[nodes.Node, str, bool]] = []  # (識別子, 種類, ループ変数か)
        loop_decls: set[int] = set()  # for (var i …) / for (var x in …) の宣言ノード（id）
        while stack:
            n = stack.pop()
            if not isinstance(n, nodes.Node):
                continue
            if n.type == "FunctionDeclaration":
                found.append((n.id, "function", False))
                continue  # 関数の中身はその関数のスコープで巻き上げる
            if n.type in FUNCTIONS or n.type in ("ClassDeclaration", "ClassExpression"):
                continue
            if n.type == "ForStatement" and n.init is not None:
                loop_decls.add(id(n.init))
            if n.type in ("ForInStatement", "ForOfStatement"):
                loop_decls.add(id(n.left))
            if n.type == "VariableDeclaration":
                loop = id(n) in loop_decls
                for d in n.declarations:
                    found.extend((ident, "var", loop) for ident in pattern_ids(d.id))
            stack.extend(children(n))
        # スタックでたどると順序が逆になるので、ソース上の順に宣言する（最初の宣言が decls に残る）
        for ident, kind, loop in sorted((f for f in found if f[0] is not None), key=lambda f: node_pos(f[0])):
            scope.declare(ident, kind)
            scope.hoisted.setdefault(ident.name, []).append((ident, kind, loop))

    def redeclare_targets(self, decls: list[tuple]) -> list[tuple]:
        """同じ名前の宣言のうち、2 重定義の判定に使うものを返す。

        ``for (var i …)`` / ``for each (var x in …)`` のループ変数を同じスコープで何度も宣言するのは
        よくあって害も少ないので、既定ではループ変数を除く。ただし関数が関わる場合
        （function 同士、function と var）はすべてを対象にする。
        options.redeclareVars が true ならループ変数も対象にする。

        Args:
            decls: 宣言の ``(識別子など, 種類, ループ変数か)`` のリスト（ソース上の順）。

        Returns:
            判定に使う宣言のリスト。2 つ以上あれば 2 重定義。
        """
        if self.options.get("redeclareVars", False) or any(k == "function" for _, k, _ in decls):
            return decls
        return [d for d in decls if not d[2]]

    def check_redeclare(self, scope: Scope) -> None:
        """no-redeclare: 同じスコープで同じ名前を 2 回以上宣言していないか調べる。

        2 つ目以降の宣言の位置に、最初の宣言の行を添えて報告する。

        Args:
            scope: 巻き上げが終わった関数スコープ。
        """
        for name, decls in scope.hoisted.items():
            targets = self.redeclare_targets(decls)
            if len(targets) < 2:
                continue
            first = targets[0][0]
            for ident, kind, _ in targets[1:]:
                what = "関数" if kind == "function" else "変数"
                self.report("no-redeclare", ident, f"{what} {name} は {first.loc.start.line} 行目でも宣言されています")

    def check_dupe_global(self, top: Scope) -> None:
        """no-dupe-global: トップレベルの宣言が、同じフォルダの別ファイルと重複していないか調べる。

        判定の対象は no-redeclare と同じ（既定ではループ変数同士の重複は報告しない）。

        Args:
            top: 巻き上げが終わったグローバルスコープ。
        """
        for name, decls in top.hoisted.items():
            others = self.siblings.get(name)
            if not others:
                continue
            # 別ファイルの宣言（辞書）も同じ形にして、自分のファイルの宣言（ノード）とまとめて判定する
            targets = self.redeclare_targets(list(decls) + [(o, o["kind"], o["loop"]) for o in others])
            mine = next((t for t in targets if isinstance(t[0], nodes.Node)), None)
            where = [t[0] for t in targets if isinstance(t[0], dict)]
            if mine is None or not where:
                continue  # 自分のファイル側か相手側が、すべてループ変数
            ident, kind, _ = mine
            files = [o["file"] for o in where]
            if self.report("no-dupe-global", ident, dupe_global_message(kind, name, files, self.siblings)):
                # 相手のファイルの行が変わったときにメッセージを作り直せるよう、元の情報も残す
                self.results[-1].update({"name": name, "kind": kind, "files": files})

    def is_loop_only(self, scope: Scope, name: str) -> bool:
        """スコープ内の name の宣言が、すべてループ変数（``for (var i …)`` など）かを返す。

        options.redeclareVars が true なら、ループ変数も特別扱いしないので常に False。

        Args:
            scope: 宣言のあるスコープ。
            name: 名前。

        Returns:
            すべてループ変数なら True。引数や catch の引数など、巻き上げた宣言でなければ False。
        """
        if self.options.get("redeclareVars", False):
            return False
        decls = scope.hoisted.get(name)
        return bool(decls) and all(loop for _, _, loop in decls)

    def check_shadow(self, scope: Scope) -> None:
        """no-shadow: 外側と同じ名前の変数・関数・引数を、内側で宣言していないか調べる。

        内側で同じ名前を宣言すると、内側からは外側の値が見えなくなる（外側の値は上書きされないが、
        外側を使っているつもりで内側を使ってしまう誤りのもとになる）。外側は、外側の関数・
        同じファイルのトップレベル・同じフォルダの別ファイルのトップレベル。
        組み込みと diet.json の globals、どちらかがループ変数だけの場合は対象外。

        Args:
            scope: 宣言を済ませた関数スコープまたは catch 節のスコープ。
        """
        what_by_kind = {"var": "変数", "function": "関数", "param": "引数", "catch": "catch の引数"}
        for name, d in scope.decls.items():
            if d["kind"] not in what_by_kind or self.is_loop_only(scope, name):
                continue
            what = what_by_kind[d["kind"]]
            hint = f"（ここでは外側の {name} を使えません）"
            s = scope.parent
            while s and name not in s.decls:
                s = s.parent
            if s:
                outer = s.decls[name]
                if outer["kind"] == "builtin" or self.is_loop_only(s, name):
                    continue
                where = "グローバル" if s.is_global else "外側の関数"
                line = outer["node"].loc.start.line
                message = f"{what} {name} は{where}の {name}（{line} 行目）と同じ名前です{hint}"
                self.report("no-shadow", d["node"], message)
                continue
            # 別ファイルの宣言の行はメッセージに入れない（lint 結果のキャッシュは別ファイルの行がずれても使い回すため）
            others = [o for o in self.siblings.get(name, []) if not o["loop"] or self.options.get("redeclareVars")]
            if others:
                files = "、".join(o["file"] for o in others)
                message = f"{what} {name} は {files} のグローバルの {name} と同じ名前です{hint}"
                self.report("no-shadow", d["node"], message)

    def enter_function(self, fn: nodes.Node, parent: Scope) -> None:
        """関数のスコープを作り、本体を走査する。

        名前付き関数式の名前・引数・arguments を宣言し、本体の var / function を巻き上げてから
        走査する。最後に未使用の変数を報告する。

        Args:
            fn: 関数ノード（FunctionDeclaration / FunctionExpression / ArrowFunctionExpression）。
            parent: 関数を定義した場所のスコープ。
        """
        scope = Scope(parent, is_function=True)
        if fn.type == "FunctionExpression" and fn.id:
            # 名前付き関数式の名前は、その関数の中でだけ見える
            scope.declare(fn.id, "fname")
        for p in fn.params:
            for ident in pattern_ids(p):
                scope.declare(ident, "param")
        # 関数内の arguments は常に使える（使わなくても報告しない）
        scope.decls.setdefault("arguments", {"node": None, "kind": "builtin", "used": True})
        body = fn.body
        if body.type == "BlockStatement":
            self.hoist(body.body, scope)
            self.check_redeclare(scope)
            self.check_shadow(scope)
            for stmt in body.body:
                self.walk(stmt, scope)
            self.check_unreachable(body.body)
        else:  # アロー関数の式本体
            self.walk(body, scope)
        for p in fn.params:  # デフォルト引数の式
            if p.type == "AssignmentPattern":
                self.walk(p.right, scope)
        self.finish_scope(scope)

    def finish_scope(self, scope: Scope) -> None:
        """スコープの走査が終わったときに、使われなかった var / function を報告する。

        グローバルスコープは、options.unusedCheckTopLevel が true のときだけ対象にする
        （トップレベルの関数は Java 側から呼ばれる入口のことが多いため）。

        Args:
            scope: 走査し終えたスコープ。
        """
        check_top = self.options.get("unusedCheckTopLevel", False)
        if scope.is_global and not check_top:
            return
        for name, d in scope.decls.items():
            if not d["used"] and d["kind"] in ("var", "function"):
                what = "関数" if d["kind"] == "function" else "変数"
                self.report("no-unused-vars", d["node"], f"{what} {name} は使われていません")

    def reference(self, ident: nodes.Node, scope: Scope, is_typeof: bool = False, is_write: bool = False) -> None:
        """識別子の参照を処理する。

        宣言が見つかれば「使用済み」にする（ただし ``x = 1`` のような代入だけでは使用とみなさない）。
        見つからず、組み込みでも typeof の対象でも with 文の中でもなければ no-undef を報告する。

        Args:
            ident: 参照している Identifier ノード。
            scope: 参照している場所のスコープ。
            is_typeof: ``typeof x`` の x かどうか（未宣言でもエラーにならないので報告しない）。
            is_write: 単純代入 ``x = …`` の左辺かどうか。
        """
        d = scope.lookup(ident.name)
        if d:
            if not is_write:
                d["used"] = True
            return
        if ident.name in self.globals or ident.name in self.siblings or is_typeof or scope.in_with():
            return
        if is_write:
            self.report("no-undef", ident, f"{ident.name} は宣言されていません（暗黙のグローバル変数になります）")
        else:
            self.report("no-undef", ident, f"{ident.name} は宣言されていません")

    # ---- 走査 -------------------------------------------------------------
    def walk(self, n: nodes.Node | None, scope: Scope) -> None:
        """ノードを再帰的にたどり、各ルールを調べる。

        前半でノード単体で判定できるルール（no-es6）を調べ、後半でノードの種類ごとに
        スコープの扱いや子のたどり方を変える。特別扱いしないノードはすべての子をたどる。

        Args:
            n: たどるノード。ノードでなければ（None など）何もしない。
            scope: ノードがあるスコープ。
        """
        if not isinstance(n, nodes.Node):
            return
        t = n.type

        # ---- no-es6: ノードの種類や属性だけで判定できるもの
        if t in ES6_NODES:
            self.report("no-es6", n, f"{ES6_NODES[t]} は Nashorn(ES5.1) では使えません")
        if t == "VariableDeclaration" and n.kind != "var":
            self.report("no-es6", n, f"{n.kind} は Nashorn(ES5.1) では使えません（var を使ってください）")
        if t in FUNCTIONS and (n.generator or n.isAsync):
            self.report("no-es6", n, "ジェネレーター / async 関数は Nashorn(ES5.1) では使えません")
        if t == "Property" and (n.shorthand or n.method or n.computed):
            self.report(
                "no-es6", n, "プロパティの省略記法 / メソッド記法 / 計算されたキーは Nashorn(ES5.1) では使えません"
            )

        # ---- ノードの種類ごとの処理（return するものは子を独自にたどる）
        if t == "Identifier":
            # ここに来る識別子は変数の参照（プロパティ名などは呼び出し元で除外している）
            self.reference(n, scope)
            return
        if t in FUNCTIONS:
            self.enter_function(n, scope)
            return
        if t == "VariableDeclarator":
            # 宣言は hoist で処理済みなので、左辺の識別子は参照として数えない
            if n.id.type != "Identifier":
                self.walk(n.id, scope)  # 分割代入のデフォルト値など（no-es6 として報告済み）
            self.walk(n.init, scope)
            return
        if t == "MemberExpression":
            # obj.prop の prop は変数ではないのでたどらない（obj[expr] の expr はたどる）
            self.walk(n.object, scope)
            if n.computed:
                self.walk(n.property, scope)
            return
        if t == "Property":
            # { key: value } の key は変数ではないのでたどらない（{ [expr]: value } はたどる）
            if n.computed:
                self.walk(n.key, scope)
            self.walk(n.value, scope)
            return
        if t == "ObjectExpression":
            self.check_dupe_keys(n)  # 子は下の共通処理でたどる
        if t in ("LabeledStatement", "BreakStatement", "ContinueStatement"):
            # ラベル名は変数ではないので、ラベル付き文の本体だけをたどる（break / continue は body なし）
            self.walk(n.body, scope)
            return
        if t == "CatchClause":
            # catch (e) の e は catch 節の中でだけ見える
            inner = Scope(scope, is_function=False)
            for ident in pattern_ids(n.param):
                inner.declare(ident, "catch")
            self.check_shadow(inner)
            self.walk(n.body, inner)
            return
        if t == "WithStatement":
            # JavaImporter と組み合わせて使われる。本体の名前は静的に解決できないので no-undef の対象外
            self.walk(n.object, scope)
            self.walk(n.body, Scope(scope, is_function=False, is_with=True))
            return
        if t == "UnaryExpression" and n.operator == "typeof" and n.argument.type == "Identifier":
            # typeof x は x が未宣言でもエラーにならない（存在確認によく使われる）
            self.reference(n.argument, scope, is_typeof=True)
            return
        if t == "AssignmentExpression" and n.left.type == "Identifier":
            # x = 1 は「使用」ではなく書き込み。x += 1 は x を読むので使用とみなす
            self.reference(n.left, scope, is_write=(n.operator == "="))
            self.walk(n.right, scope)
            return
        if t == "BinaryExpression" and n.operator in ("==", "!="):
            # options.eqeqeqAllowNull が true なら x == null / x != null は許可する
            allow_null = self.options.get("eqeqeqAllowNull", False)
            is_null = any(s.type == "Literal" and s.value is None and s.raw == "null" for s in (n.left, n.right))
            if not (allow_null and is_null):
                strict = n.operator + "="
                self.report("eqeqeq", n, f"{n.operator} ではなく {strict} を使ってください（型変換による誤判定の恐れ）")
        if t == "DebuggerStatement":
            self.report("no-debugger", n, "debugger 文が残っています")
        if t in ("BlockStatement", "SwitchCase"):
            self.check_unreachable(n.body if t == "BlockStatement" else n.consequent)

        # ---- 共通処理: すべての子をたどる
        for c in children(n):
            self.walk(c, scope)

    # ---- 個別ルール ---------------------------------------------------------
    def check_dupe_keys(self, obj: nodes.Node) -> None:
        """no-dupe-keys: オブジェクトリテラルのキーの重複を調べる。

        同名の getter と setter の組（``get x(){}`` と ``set x(v){}``）は重複とみなさない。

        Args:
            obj: ObjectExpression ノード。
        """
        seen: dict[str, set[str]] = {}  # キー名 -> 出てきた kind（"init" / "get" / "set"）の集合
        for p in obj.properties:
            if p.type != "Property" or p.computed:
                continue  # 計算されたキーは値がわからないので対象外
            k = p.key
            # {a: 1} と {"a": 1} と {1: 1} を同じ扱いにするため、文字列にそろえる
            name = k.name if k.type == "Identifier" else str(k.value)
            kinds = seen.setdefault(name, set())
            # 同じ kind が 2 回目、または通常のキー（init）と何かが重なれば重複
            if p.kind in kinds or (p.kind == "init" and kinds) or ("init" in kinds):
                self.report("no-dupe-keys", k, f"キー {name} が重複しています")
            kinds.add(p.kind)

    def check_unreachable(self, stmts: list[nodes.Node]) -> None:
        """no-unreachable: return / throw / break / continue の後にある文を報告する。

        巻き上げられる function 宣言と、初期値のない var 宣言は到達できなくても意味があるので除く。
        報告は最初の 1 文だけ。

        Args:
            stmts: 同じブロック内の文のリスト。
        """
        for i, s in enumerate(stmts):
            if s.type in TERMINATORS:
                for nxt in stmts[i + 1 :]:
                    if nxt.type == "FunctionDeclaration":
                        continue
                    if nxt.type == "VariableDeclaration" and all(d.init is None for d in nxt.declarations):
                        continue
                    self.report("no-unreachable", nxt, "到達できないコードです")
                    break
                return
