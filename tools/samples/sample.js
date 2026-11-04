var ArrayList = Java.type("java.util.ArrayList");

function render(user, items) {
    var list = new ArrayList();
[DIEt]<h1>タイトル</h1>[/DIEt]
    if (user) [DIEt]<p>ようこそ</p>[/DIEt]
    else [DIEt]<p>ゲスト</p>[/DIEt]
[DIEt]<ul>[/DIEt][DIEt]<li>先頭</li>[/DIEt]
    for (var i = 0; i < items.length; i++) {
        list.add(items[i]);
[DIEt]<li>"引用符"や */ を含む</li>[/DIEt];
    }
[DIEt]
    <footer>
      複数行
    </footer>
  [/DIEt]
    var unused = 1;
    if (list.size() == 0) {
        print("empty");
    }
    return list;
}
