# DIEt対応 JS lint / format（Python のみ・exe 不要・Node 不要）

jsbeautifier（整形）＋ esprima（構文解析）＋ 自前のリンターで、`[DIEt]…[/DIEt]` を含む
Nashorn 向け JS を整形・lint します。必要なのは Python と pip パッケージだけです。

## フォルダ構成
```
diet-lint/
├─ .vscode/            tasks.json（タスク）/ settings.json（保存時に実行）
├─ diet_templates/     運用する JS（ここに置いたファイルを保存すると整形＋lint）
├─ tools/
│  ├─ diet_tool.py     入口（format / lint / check / save）
│  ├─ diet_lint.py     リンター本体
│  ├─ diet.json        設定（整形オプション・globals・ルール）
│  ├─ requirements.txt / requirements-dev.txt（開発用。ruff を追加）
│  └─ samples/         動作確認用の JS（diet_templates とはフォルダを分けて、同じフォルダの判定に混ぜない）
├─ .venv/              Python の仮想環境（セットアップで作成）
└─ ruff.toml           Python コードの lint / format 設定（開発時のみ）
```
- 「同じフォルダ」の判定（別ファイルの定義、2重定義、フォルダ単位の lint）は JS ファイルのあるフォルダごと。
  diet_templates の下にサブフォルダを作ると、サブフォルダごとに別のまとまりとして扱う
- 保存時タスクが動くのは `diet_templates/**/*.js` と `tools/samples/**/*.js`（.vscode/settings.json）

## セットアップ
1. Python 3.10 以上（`python` コマンドが PATH から使えること。venv の作成にだけ使う）
2. VSCode のタスク「DIEt: セットアップ（パッケージ導入）」を実行する
   - ワークスペース直下に `.venv` を作り（「DIEt: venv 作成」）、そこに tools/requirements.txt のパッケージを入れる
   - グローバルの Python にはパッケージを入れない。整形・lint のタスクは `.venv/Scripts/python.exe` で動く
   - 手動でやる場合: `python -m venv .venv` → `.venv\Scripts\python -m pip install -r tools/requirements.txt`
   - プロキシ環境: `HTTPS_PROXY` を設定するか、pip に `--proxy http://host:port` を付ける
   - オフライン: 別PCで `pip download -r tools/requirements.txt -d wheels` →
     `.venv\Scripts\python -m pip install --no-index --find-links wheels -r tools/requirements.txt`
   - ツール自体を開発する（ruff を使う）場合は tools/requirements-dev.txt を入れる
3. VSCode に Trigger Task on Save 拡張（Gruntfuggly.triggertaskonsave）を入れる
   - `code --install-extension Gruntfuggly.triggertaskonsave`、または拡張機能ビューで検索してインストール
   - インストール後に「Developer: Reload Window」
   - 保存するたびに整形＋lint が走り、結果が「問題」パネルに出る
   - VSCode 標準の JS チェックは [DIEt] タグを誤検出するので、.vscode/settings.json でオフにしてある

## 使い方
```
.venv\Scripts\python tools/diet_tool.py format a.js b.js   # 整形して上書き
.venv\Scripts\python tools/diet_tool.py lint   a.js        # lint（行番号は元ファイルと一致）
.venv\Scripts\python tools/diet_tool.py check  a.js        # 整形が必要なら終了コード1（CI向け）
.venv\Scripts\python tools/diet_tool.py save   a.js        # 整形してから lint（保存時タスクが使用）
```

## lint ルール（diet.json の rules で error / warning / off）
| ルール | 内容 |
|---|---|
| parse | 構文エラー（常に有効） |
| no-es6 | let/const、アロー関数、テンプレートリテラル、class、分割代入、for...of など ES2015 以降の構文 |
| no-undef | 宣言されていない変数の参照・代入（暗黙のグローバル） |
| no-unused-vars | 未使用の変数・関数（既定では関数内のみ。トップレベルは外部から呼ばれる想定で対象外） |
| eqeqeq | == / != |
| no-dupe-keys | オブジェクトのキー重複 |
| no-unreachable | return 等の後の到達不能コード |
| no-debugger | debugger 文 |
| no-redeclare | 同じファイル・同じスコープでの2重定義 |
| no-dupe-global | 同じフォルダの別ファイルとの、トップレベルの2重定義 |
| no-shadow | 関数の中で、外側（外側の関数・グローバル・同じフォルダの別ファイルのグローバル）と同じ名前の変数・関数・引数を宣言している |
| semi | 文末の `;` がない（整形で自動的に補う） |

- Java 連携で使う独自のグローバル（Java側から注入する変数など）は diet.json の `globals` に追加
  （既定で `dsm`, `dtm`, `$` を登録済み）
- **同じフォルダにある別の .js ファイル**でトップレベルに定義された関数・変数は、宣言なしで使える名前として扱う
  （`options.siblingGlobals`。false で無効）
  - 解析結果は `.diet_cache.json` にキャッシュし、更新されたファイルだけ解析し直す（消しても自動で作り直す）
- lint は、指定したファイルと**同じフォルダの .js をすべて**対象にする（`options.lintScope`。`"file"` で指定したファイルだけ）
  - VSCode のタスクは実行のたびに前回の「問題」を消すため、保存したファイルだけを lint すると
    別のファイルを保存したときに前のファイルの結果が消えてしまう。毎回フォルダ全体の結果を出すことで残す
  - 変わっていないファイルは前回の lint 結果を使うので、フォルダ全体でも速い
- 保存時の整形でファイルが書き換わった場合は、lint 結果の出力を少し待つ（`options.reloadWaitMs`、既定 800 ミリ秒）
  - VSCode がファイルを読み込み直すときに、変わった行の波線を動かしてしまうため。読み込み直しが終わってから
    結果を出せば、波線は正しい位置に出る。それでも波線がずれる場合は値を大きくする（0 で待たない）
- no-redeclare / no-dupe-global は、`for (var i …)` / `for each (var x in …)` のループ変数を除いて判定する
  （ループ変数を何度も宣言するのはよくあって害も少ないため）。関数が関わる重複はループ変数も含めて報告する。
  ループ変数の重複も報告したい場合は `options.redeclareVars` を true にする
  - 例: 2つのファイルに `var test = 1;` → 報告する / 2つのファイルに `for (var i = 0; …)` → 報告しない
- semi: 代入・関数呼び出し・var・return・throw・break・continue・do…while・debugger の文末に `;` がなければ報告し、
  整形（保存時を含む）で `;` を補う。semi を `"off"` にすると報告も自動修正もしない
  - `[DIEt]` タグの後ろの `;` は任意のまま。`$(x)[DIEt]…` のように同じ行でタグが続く書き方も対象外
  - `for (var i = 0; …)` の var は対象外
- no-shadow: 関数の中の `var test` はグローバルの `test` を上書きしない（別の変数になる）が、関数の中から
  グローバルの `test` が見えなくなり、取り違えの原因になるので警告する。
  組み込み・diet.json の globals と同じ名前、どちらかがループ変数の場合は対象外（redeclareVars が true なら対象）
- `typeof x === "undefined"` のような存在確認は no-undef の対象外
- `with (new JavaImporter(...)) { ... }` の本体は no-undef の対象外（名前を静的に解決できないため）

## 仕組み
- `[DIEt]文字列[/DIEt]` → `__DIET__(番号);` に置換して処理し、結果を元に戻す
- 文として扱われるので `if (x) [DIEt]…[/DIEt]` のような波括弧なしの書き方でも壊れない
- lint 時は中身が複数行でも行数を維持するので、行番号がずれない
- 中身の文字列は退避するので、引用符や `*/` を含んでいても問題なし。直後の `;` の有無も保持
- `$(x)[DIEt]…` のように同じ行のコードとタグの間に `;` がない書き方は、内部で `;` を補って解析する（整形結果では元に戻す）
- 同じ行に並んだタグとコード（`[DIEt]a=[/DIEt]print(x);[DIEt]b[/DIEt]` など）は、整形後も同じ行のまま
- 行頭にあるタグのインデントは diet.json の `options.tagIndent` で指定する
  - `"never"`（既定）: インデントしない（常に行頭）
  - `"keep"`: 元ファイルのインデントのまま
  - `"format"`: 周りのコードと同じように整形でインデントする
- 構文エラーがあるファイルは整形しない（jsbeautifier は構文エラーでも整形してしまうため、先に esprima で確認）

## 注意
- Nashorn の `for each (x in y)` は対応済み（lint では "each" を同じ長さの空白に置き換えて for-in として解析。
  整形結果は `for each (` に揃える）
- それ以外の Nashorn 独自構文（式クロージャ `function(x) x*x`、-scripting の `#` コメント等）は構文エラー扱いになる
- jsbeautifier はセミコロンの補完などはしない（インデント・空白・改行の整形のみ）
- 改行コードは元ファイルに合わせる（diet.json の format.eol = "auto"）
