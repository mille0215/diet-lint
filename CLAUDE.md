# CLAUDE.md — DIEt対応 JS lint / format（Python のみ）

Node.js が使えない環境で、Nashorn 向け JavaScript（独自タグ `[DIEt]…[/DIEt]` を含む）を
整形・lint する仕組み。claude.ai のチャットで方針を決め、動作するプロトタイプまで作成済み。以下はその引き継ぎ。

## 前提・制約（ユーザー確認済み）
- **Node.js は禁止**。Node を別途必要とするVSCode拡張・ツール（ESLint拡張、Prettier等）も使えない
- VSCode・VSCode拡張・Python はインストール可。ネット接続あり、プロキシ経由も可
- 実行環境の JS エンジンは **Nashorn**（ES5.1 前提）
- 独自タグ `[DIEt]…[/DIEt]`
  - 中身は単なる文字列（囲んだ文字列をそのまま出力するだけ）
  - 基本はブロック（文）単位。1行に複数書くこともある
  - 実行前に正しいJSへ変換されるため、`if (x) [DIEt]…[/DIEt]` のような波括弧なしの書き方もありうる
- タグを含むファイルが多い
- **方針: exe を使わず Python だけで実現する**（社内で文句を言われにくいため。ユーザー決定）

## 経緯（不採用にした案）
- Biome（Rust製単体exe）＋Pythonラッパーで一度作成・検証済みだったが、biome.exe が社内のソフトウェア申請対象に
  なりうること、VCRUNTIME140.dll 依存があることから、Python のみの構成に切り替えた
  - 補足: 現行の Biome VSCode拡張（v3.8.1）はバイナリを自動ダウンロードしない（ソースで確認）
- Closure Compiler（Java jar）、mini-racer/quickjs で ESLint を動かす案も不採用

## 構成
- フォルダ構成（2026-11-05 変更。以前はすべてルート直下）
  - `tools/` … ツール一式（diet_tool.py, diet_lint.py, diet.json, requirements.txt）。diet.json と
    `.diet_cache.json` は diet_tool.py と同じフォルダ（`Path(__file__)` 基準）なので、tools/ を丸ごと移せば動く
  - `tools/samples/` … 動作確認用 JS（sample.js, test.js, test2.js）。運用 JS と同じフォルダにすると
    別ファイルの定義・2重定義の判定に混ざるので分けている
  - `diet_templates/` … 運用する JS を置く場所（ユーザー決定）
  - 保存時タスクの対象は settings.json で `diet_templates/**/*.js` と `tools/samples/**/*.js` に限定
  - Python パッケージはワークスペース直下の `.venv` に入れる（2026-11-05、ユーザー決定。以前はグローバル）。
    tasks.json の整形・lint タスクは `${workspaceFolder}/.venv/Scripts/python.exe` を呼ぶ（Windows 前提のパス）。
    セットアップタスクは「DIEt: venv 作成」（PATH の python で `-m venv`）に dependsOn してから pip install。
    開発用は tools/requirements-dev.txt（requirements.txt ＋ ruff）。settings.json の python.defaultInterpreterPath も .venv
  - ruff.toml に `src = [".", "tools"]`（diet_lint を自前モジュールとして import 順を判定させるため）
- `diet_tool.py` … 入口。`format` / `lint` / `check`（CI向け、変更しない）/ `save`（整形→lint、保存時タスク用）
  - 整形: jsbeautifier（設定は diet.json の "format"。jsbeautifier のオプション名そのまま）
  - 整形前に esprima で構文確認し、構文エラーなら整形しない（jsbeautifier は壊れたコードも整形するため）
  - lint 結果は GitHub Actions 形式（`::error title=ルール,file=…,line=…,endLine=…,col=…,endColumn=…::メッセージ`）で出力
    → `.vscode/tasks.json` の problemMatcher が「問題」パネルに表示
  - lint の終了コード: error レベルがあれば 1
- `diet_lint.py` … esprima(4.0.1) の AST を使う自前リンター。スコープ解析あり（var/function の巻き上げ、
  引数、名前付き関数式、catch 引数、arguments）。ルール: parse, no-es6, no-undef, no-unused-vars, eqeqeq,
  no-dupe-keys, no-unreachable, no-debugger, no-redeclare, no-dupe-global, no-shadow, semi。メッセージは日本語
  - BUILTINS に ES5.1 と Nashorn の組み込み（Java, Packages, print, load など）と `__DIET__` を登録
  - no-unused-vars はトップレベルを既定で対象外（Java 側から呼ばれる入口関数のため。options.unusedCheckTopLevel）
  - 代入のみ（`x = 1`）は「使用」と見なさない。`typeof x` は no-undef の対象外
- `diet.json` … 整形オプション、追加 globals、ルールのレベル、オプション
- `ruff.toml` … Python コードの lint / format 設定（2026-11-04 追加）。`.venv/Scripts/python -m ruff check .` と
  `.venv/Scripts/python -m ruff format --check .` が通る状態を保つ（ruff は開発時のみ使用、tools/requirements-dev.txt）
  - 行長 120、既定ルール＋ E/W/I/B/UP/SIM/D（docstring は Google スタイル、日本語で書く）
  - D415（1行目を "." で終える）は日本語の「。」と合わないので無効
  - BUILTINS の集合はグループごとに横並びで読みやすくするため `# fmt: off` / `# fmt: on` で囲んでいる
- `requirements.txt` … jsbeautifier>=1.15, esprima==4.0.1
  - 注意: esprima-python の GitHub main ブランチは import エラーで壊れている（HEX_CONV）。PyPI の 4.0.1 / タグ v4.0.1 は正常
- `.vscode/tasks.json` … 保存時（整形＋lint）/ 整形 / lint / セットアップ（pip install）
- `.vscode/settings.json` … Trigger Task on Save（Gruntfuggly.triggertaskonsave）で保存時に `save` タスクを実行
- `tools/samples/sample.js` … タグの各パターンを含む動作確認用サンプル

## タグ置換の仕様
- `[DIEt]中身[/DIEt]` → `__DIET__(番号);`（文として扱われるので波括弧なし if/else でも安全）
- lint 時のみ、中身が複数行なら `__DIET__(番号/*\n\n*/);` として改行数を保持 → 行番号が元ファイルと一致
  （整形時は付けない。jsbeautifier がコメント内の改行を崩すため）
- 中身は配列に退避するので、引用符や `*/` を含んでも壊れない
- タグ直後の同一行の `;` の有無を記録して復元
- 元ファイルで同じ行にあったタグとコード（`[DIEt]a=[/DIEt]print(x);[DIEt]b[/DIEt]` など）は整形後も同じ行に保つ。
  mask 時に各タグの前後の同一行の空白（pre_sep / post_sep）を記録し、unmask 時に jsbeautifier が入れた改行を
  その空白に戻す（2026-11-04 変更。以前は1行1文に分かれていた）
- 行頭にあるタグのインデントは diet.json の options.tagIndent で制御（ユーザー要望で既定は "never"）
  - "never": インデントしない（常に行頭） / "keep": 元ファイルのインデントのまま / "format": jsbeautifier に合わせる
  - mask で各タグの元のインデント（indent。前に同じ行のコードがあれば None）を記録し、unmask はタグを1つずつ
    前から処理するループで、直前の空白のうち最後の改行より後（＝整形で付いたインデント）を置き換える
  - 整形で前の行のコードとつながった場合（`else\n[DIEt]` → `else [DIEt]` など）はそのまま
- 復元後に `__DIET__` が残っていたら例外にしてファイルを書かない

## 検証済み（Linux、jsbeautifier 2.0.3 / esprima 4.0.1、GitHub のソースから実行）
- sample.js の整形 → タグ位置・インデントが正しく復元、2回目の整形で差分なし、check が正しく判定
- lint の行番号が元ファイルと一致（複数行タグを含むケース）
- 各ルールの検出と、誤検出しないケース（typeof、catch 引数、名前付き関数式、引数、get/set の同名キー）
- 構文エラー時にファイルが変更されず、parse エラーが報告される

## 未検証・未解決
- Windows＋VSCode 上での実地確認は 2026-11-04 に完了（下の「Windows＋VSCode での実地確認」参照）
- 実コードでの誤検出の確認とルール調整（特に no-undef: Java 側から注入されるグローバルを diet.json に追加する必要）
- `for each (x in y)` は**使用している（ユーザー確認済み）ので対応済み**:
  diet_lint.py の strip_nashorn_ext で "each" を同じ長さの空白に置換して for-in として解析（行・列は不変）。
  jsbeautifier は for each をそのまま整形できる（出力 "for each(" を diet_tool.py で "for each (" に揃える）
- それ以外の Nashorn 独自構文（式クロージャ、`#` コメント等）は未対応。使っていれば同様の前処理を追加
- jsbeautifier の整形スタイルをチームの好みに合わせる
  - brace_style は "collapse"。"collapse,preserve-inline" だと1行ブロック内のタグで整形が冪等にならなかった
- 保存時に整形でファイルが書き換わるため、エディタの再読込との競合の可能性（気になれば save を lint のみに）
- 必要ならルール追加（例: no-empty）

## 同じフォルダの別ファイルの定義と2重定義（2026-11-04 追加、ユーザー要望）
- diet_tool.py の sibling_decls が、lint 対象と同じフォルダの他の `*.js` のトップレベル var / function を集め
  （diet_lint.top_level_decls）、Linter(config, siblings) に渡す。siblings にある名前は no-undef にしない。
  options.siblingGlobals（既定 true）で無効化可。サブフォルダや load() 先は対象外
- 新ルール（既定 warning）
  - no-redeclare: 同じファイル・同じ関数スコープでの重複宣言。hoist が全宣言を Scope.hoisted に残す
  - no-dupe-global: 別ファイルとのトップレベルの重複。「b.js（3 行目）」のように相手の位置を表示
  - どちらも既定ではループ変数（`for (var i…)` / `for each (var x in…)` で宣言した var）を判定から除く。
    関数が関わる重複はループ変数も含める。options.redeclareVars: true でループ変数も対象
    （当初は「関数が関わる重複のみ」にしていたが、`var test = 1;` の重複が出ないとユーザーから指摘されて変更）
  - hoist が各宣言にループ変数かどうかを記録（Scope.hoisted の3要素目）。top_level_decls は名前ごとに
    "loop"（すべてループ変数か）を返す。キャッシュの形式が変わったので DECLS_CACHE_VERSION を 2 にした
  - `var ArrayList = Java.type(...)` を複数ファイルに書くと報告される（同じ値なので実害は小さいが、仕様として許容）
- 速度対策
  - insert_semicolons は以前「構文エラーの位置で1つずつ補って再解析」で、`$(x)[DIEt]` が150か所ある
    2000行のファイルで3.6秒かかっていた。tokenize 1回で補う位置を決める方式（needs_semicolon）に変更し、
    旧方式は判定しきれない場合の予備として残した
  - 別ファイルの宣言は `.diet_cache.json`（ツールのフォルダ）に、ファイルの更新日時・サイズと一緒にキャッシュ
  - 2000行×30ファイルのフォルダで lint 1回: 113秒 → 5.0秒（キャッシュなし）→ 0.74秒（キャッシュあり）

## フォルダ単位の lint と結果のキャッシュ（2026-11-05 追加）
- 問題: VSCode のタスクは実行のたびに、同じ problemMatcher（owner）の前回の「問題」を消す。保存したファイルだけを
  lint していたので、別のファイルを保存すると前のファイルの警告が消えた（ユーザー指摘）
- 対策: lint / save は、指定ファイルと同じフォルダの *.js をすべて lint して出力する（lint_targets）。
  options.lintScope: "file" で従来どおり指定ファイルだけ。整形は保存したファイルだけ
- キャッシュ（diet_tool.py の Cache クラス、`.diet_cache.json`、CACHE_VERSION=3）
  - ファイルごとに stamp（更新日時・サイズ）、decls（top_level_decls の結果）、lint（条件のハッシュと結果）
  - lint 結果を使い回す条件: 自分の stamp が同じ、かつ「別ファイルの宣言（行番号を除く）・globals・rules・
    options・ツールのコード（TOOL_HASH = diet_tool.py と diet_lint.py のハッシュ）」のハッシュが同じ
  - 行番号を条件から外しているのは、どこかのファイルに1行足すだけで全ファイルを lint し直さないため。
    行番号を使う no-dupe-global のメッセージは、結果に name / kind / files を残しておき、出力前に
    dupe_global_message で作り直す
- 2000行×30ファイルのフォルダで: 初回 10秒 / 変更なし 1.2秒 / 1ファイルを変更（行の追加を含む）1.5秒

## no-shadow（2026-11-05 追加、ユーザー要望）
- 関数スコープ（引数・var・function）と catch 節で宣言した名前が、外側の関数・同じファイルのグローバル・
  同じフォルダの別ファイルのグローバルにもあれば報告（既定 warning）。check_shadow
- 対象外: 組み込み（BUILTINS）と diet.json の globals、名前付き関数式の名前、どちらかがループ変数だけの場合
  （is_loop_only。redeclareVars: true なら対象）
- 別ファイル相手のメッセージには行番号を入れない（lint 結果のキャッシュは別ファイルの行がずれても使い回すため）
- ユーザーは「上書きされる」と考えていたが、実際は関数内の var は別の変数で外側は上書きされない。
  取り違えの原因になるので警告する、とユーザーに説明した

## 実コード相当での lint 調整（2026-11-04 実施）
- 対象: 隣接プロジェクトのテンプレート（`../dietcode2spec/sample*.js`、`../graaljs_test/diet_templates/*.js`）。
  社外に出せない実テンプレートは手元にないため、これらで代用。`convert.js` / `test/regression.js` は Node 用ツールなので対象外
- 仕様の出典: `../dietcode2spec/HANDOVER.md`（出力手段は `dsm.dwrite` / `dsm.dwriteln` / `$(…)`、
  `$("")[DIEt]` のような `;` なしの書き方あり、js 間の参照なし）、`../graaljs_test`（本番は `dtm` / `dsm` を注入）
- 対応内容
  - diet.json の globals に `dsm`, `dtm`, `$` を追加
  - BUILTINS に Nashorn の `arguments`（トップレベル）, `importPackage`, `importClass`, `$ARG`, `$ENV`, `$EXEC`,
    `$OUT`, `$ERR`, `$EXIT`, `$OPTIONS` を追加
  - `with` 文の本体は no-undef 対象外（JavaImporter と併用されるため）
  - `;` なしでコードの直後にタグが続く書き方（`$(x)[DIEt]`）: diet_tool.py の insert_semicolons が、構文エラー位置が
    プレースホルダ先頭かつ同じ行の前にコードがあるときだけ `;` を補って再解析（convert.js と同じ方式）。
    一律に補うと `if (x) [DIEt]` が空文になるため。整形の復元時はこの `;` を取り除く
- 意図的に globals へ入れていないもの: `ctx`, `master`（HANDOVER で架空と明記）、`util`（load した別ファイル由来）、
  `$utl`（sample4.js にあるが実在か不明）。実在するなら diet.json に追加
- eqeqeq（warning）は Java の値と文字列の `==` 比較で多数出る。実コードで多すぎるなら off を検討
- jsbeautifier は `with (` を `with(` と出力する（for each のような補正はしていない）

## Windows＋VSCode での実地確認（2026-11-04 実施、Windows 11 / Python 3.15.2）
- `python` コマンドが PATH にあることが前提（venv 作成タスクが `python` を呼ぶ）。`py` しかない環境では動かない
- セットアップ（`python -m pip install -r requirements.txt`）→ esprima 4.0.1 / jsbeautifier 2.0.3 が入る
- Trigger Task on Save（Gruntfuggly.triggertaskonsave v0.2.17）は**手動インストールが必要**
  （`code --install-extension Gruntfuggly.triggertaskonsave`）。インストール後は Reload Window。
  タスクはラベルの完全一致で探すので、settings.json のキーと tasks.json の label を揃えること
- 保存 → 整形 → lint → 問題パネルへの表示を確認。行・列は正しく、`$(x)[DIEt]` も `;` なしのまま復元された
- **VSCode 標準の JS チェック（source: ts）が [DIEt] タグを構文エラーとして誤検出する**ため、settings.json で
  `js/ts.validate.enabled`（新しい VSCode）と `javascript.validate.enable`（古い VSCode）を false にした
- 確認用に test.js に整形前のコードを書いた（現在は tools/samples/。不要なら削除してよい）

## semi（2026-11-05 追加、ユーザー要望: lint でエラー、整形で自動修正）
- diet_lint.missing_semicolons: SEMI_STATEMENTS（式文・var・return・throw・break・continue・do…while・debugger）で
  range の最後の文字が ";" でないものを集める。for の初期化部・for-in の左辺の var は除く。プレースホルダの文は除く
- lint: 文末の位置（at_end=True）に error で報告。整形: diet_tool.format_file が add_semicolons で masked に ";" を補ってから
  jsbeautifier に渡す（rules.semi が "off" なら補わない）
- 補う順番は「文の終わりの位置」の降順。開始位置の順だと入れ子の文（関数式の中の return と外側の var、do の中の
  a++ と do…while）で位置がずれ、`return a;;` や `while (a < 3;)` になった（修正済み）
- `$(x)[DIEt]` は insert_semicolons が ";" を補った状態で解析するので semi の対象にならず、復元時も ";" なしに戻る
- 波線のずれ（2026-11-05、ユーザー指摘）: semi の自動修正などで保存時にファイルを書き換えると、no-shadow などの
  波線がずれた。lint 結果（新しい内容に対して正しい位置）が先に問題パネルに入り、そのあと VSCode がファイルを
  読み込み直すときに、変わった行の問題の位置を編集に合わせて動かすためと考えている。
  対策として save で書き換えがあったときだけ、書き換えから options.reloadWaitMs（既定 800ms）たってから
  lint 結果を出力する（wait_for_editor_reload。lint の時間は差し引く）。待ち時間が足りるかは環境しだい

## 次にやること（候補）
1. 社用 PC で同じ手順を確認（Python の PATH、拡張のインストール、プロキシ経由の pip install）
2. 社用 PC の実テンプレートに lint を流し、残る no-undef（システム提供の独自オブジェクト）を globals に追加
3. for each 以外の Nashorn 独自構文の使用有無を確認
