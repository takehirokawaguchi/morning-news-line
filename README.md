# morning-news-line

自分用の「朝刊」を毎朝LINEに配信するGitHub Actionsワークフローです。

技術系ニュース(Qiita/Zennトレンド、Hacker News)に加えて、世界情勢・金融経済・
一般ニュース(BBC News、Google Newsキーワード検索)など、普段自分から追わない
ジャンルも含めて幅広く収集し、Gemini API (`gemini-3.6-flash`、無料枠内で利用) で
日本語3〜4行に要約した上で、LINE Messaging APIのpush messageで毎朝8:00 (JST) に
配信します。

## 配信内容

- **技術系: 3件**(英語ソースは最大1件、残り2件以上は日本語ソース)
  - ソース: Qiita トレンド, Zenn トレンド, Hacker News
- **技術系以外: 7件**(英語ソースは最大3件、残り4件以上は日本語ソース)
  - 世界情勢・金融経済・一般ニュースを広くカバー
  - ソース: BBC News(World/Business)、Google Newsキーワード検索(無料・APIキー不要。
    ロイター・共同通信・日経・APなど多数の媒体を横断的に拾える)

要約は以下の方針でGemini APIに指示しています(`scripts/send_news.py` の
`BASE_SYSTEM_PROMPT` / `WORLD_AFFAIRS_ADDENDUM` を参照):

- 元記事が英語でも要約は必ず日本語
- 誇張・主観を含めず事実ベース
- **世界情勢に関する記事は、特定の立場に偏らないフラットな要約**とし、対立する
  見方がある場合はその旨も簡潔に触れる
- 各記事の末尾に「出典: ○○(英語記事/日本語記事)」を明記(要約後にスクリプト側で付与)

過去に配信済みの記事URLは `data/sent_urls.json` にキャッシュされ(直近30日分を保持)、
同じ記事が翌日以降に再配信されないようになっています。ワークフロー実行後、
差分があれば自動でこのファイルをコミット・pushします。

## セットアップ

### 1. 必要なGitHub Secretsの設定

このリポジトリの `Settings > Secrets and variables > Actions` から、以下の
Secretsを登録してください(**コード内には絶対にハードコードしないでください**)。

| Secret名 | 内容 |
| --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Messaging APIのチャネルアクセストークン(長期) |
| `LINE_USER_ID` | 配信先のLINEユーザーID(自分自身のuserId) |
| `GEMINI_API_KEY` | Gemini APIキー(無料枠。取得方法は下記) |
| `GOOGLE_NEWS_KEYWORDS_JSON`(任意) | 検索キーワードを非公開にしたい場合に設定。詳細は「4. 検索キーワードを非公開にしたい場合」参照 |

**Gemini APIキーの取得方法**: [Google AI Studio](https://aistudio.google.com/apikey)
にGoogleアカウントでログインし、「Create API key」から発行します。クレジットカード
登録は不要で、無料枠の範囲内であれば課金は発生しません。無料枠のレート制限
(1分あたり/1日あたりのリクエスト数上限)は変更されることがあるため、キー発行時に
AI Studio上の最新の制限を確認してください。このツールは1日1回・最大10件の要約
リクエストしか送らないため、無料枠を通常の使い方で超えることはまずありません。
万一無料枠の上限に達した場合は、課金設定をしていない限り追加料金は発生せず、
API側がHTTP 429(レート制限エラー)を返してその日の実行は失敗として終了します。

### 2. LINE Messaging APIのチャネル作成手順(概要)

1. [LINE Developers](https://developers.line.biz/) にログインし、プロバイダーを作成
2. そのプロバイダー配下に「Messaging API」チャネルを新規作成
3. 作成したチャネルの「Messaging API設定」タブから以下を取得
   - **チャネルアクセストークン(長期)**: 発行ボタンから発行 → `LINE_CHANNEL_ACCESS_TOKEN`
4. 自分のLINEアプリで、作成したチャネルの公式アカウントを友だち追加
   (QRコードがMessaging API設定タブに表示されます)
5. 自分の `userId` を取得する方法の一例:
   - チャネルの「Messaging API設定」で Webhookを一時的に有効化し、自分のLINEアプリ
     からそのアカウントに何かメッセージを送ると、Webhookのイベントペイロードに
     含まれる `source.userId` が自分のuserIdです(簡易的な確認用スクリプトや
     [LINE Notify代替の各種ツール] を使っても構いません)
   - もしくはLINE Developersコンソールの自分のプロフィール設定にある「userID」を
     使う方法もあります(グループ・複数人トークではなく1:1トークに送る場合)
6. 取得した `userId` を `LINE_USER_ID` として登録
7. 応答メッセージ機能は不要なので、Messaging API設定で「応答メッセージ」をオフ、
   「Webhookの利用」は取得作業が終わったらオフに戻して構いません

### 3. 記事構成のルール(件数・言語比率)を変更したい場合

`scripts/send_news.py` 内の `SELECTION_RULES` を編集してください。

```python
SELECTION_RULES = {
    "tech": {"total": 3, "max_english": 1},
    "nontech": {"total": 7, "max_english": 3},
}
```

- `total`: そのセクションの記事件数
- `max_english`: そのうち英語ソースを許容する最大件数(残りは日本語ソースで補完)

技術系ニュースソース自体を変更・追加したい場合は、同ファイルの `fetch_hackernews` /
`fetch_qiita_trend` / `fetch_zenn_trend` / `collect_source_pools` を編集してください。
RSSフィードを追加する場合は `fetch_rss(url, source, language, category)` を
呼び出すだけで組み込めます。

技術系以外のニュースを広げているのは `DEFAULT_GOOGLE_NEWS_KEYWORDS` の検索
キーワードリストです。各要素は `query`(検索語)/ `language`(`ja` または `en`) /
`category`(`world` / `finance` / `general`)を持ち、自由に追加・削除・編集できます。
`category` に `world` を指定した記事にのみ「フラットな要約」の追加指示が
プロンプトに付与されます。

キャッシュの保持期間を変更したい場合は `CACHE_RETENTION_DAYS` を編集してください。

### 4. 検索キーワードを非公開にしたい場合

`DEFAULT_GOOGLE_NEWS_KEYWORDS` はコードに直接書かれているため、パブリック
リポジトリでは誰でも閲覧できます。実際に検索したいキーワード(=自分の興味関心)を
公開したくない場合は、`GOOGLE_NEWS_KEYWORDS_JSON` というSecretsに、同じ形式の
JSON配列を丸ごと設定してください。設定されていればそちらが優先され、コード内の
デフォルトは使われなくなります。

```json
[
  {"query": "本当に追いたいキーワード", "language": "ja", "category": "world"},
  {"query": "another keyword", "language": "en", "category": "finance"}
]
```

未設定・JSONとして不正な場合は、自動的に `DEFAULT_GOOGLE_NEWS_KEYWORDS` に
フォールバックします(ログにその旨が出力されます)。

## ローカルでのテスト実行方法

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export LINE_CHANNEL_ACCESS_TOKEN="xxxx"
export LINE_USER_ID="xxxx"
export GEMINI_API_KEY="xxxx"
# 任意: 独自の検索キーワードを使う場合
# export GOOGLE_NEWS_KEYWORDS_JSON='[{"query": "...", "language": "ja", "category": "world"}]'

python scripts/send_news.py
```

正常終了すると `data/sent_urls.json` が更新されます。テスト目的で何度も実行する
場合は、このファイルを一時的に退避・復元するか、テスト用に別ファイルを指定する
よう `CACHE_PATH` を書き換えて実行してください。

実行ログには記事タイトルや件数などの範囲でのみ情報を出力し、記事本文全文や
APIキー・アクセストークンなどの機密情報は出力しません。

## GitHub Actionsでの実行

- `.github/workflows/daily-news.yml` が毎朝8:00 (JST) に自動実行されます
  (cronはUTC基準のため `0 23 * * *` = UTC 23:00 = JST翌8:00 と設定しています)
- `Actions` タブから `Daily Morning News to LINE` を選択し、`Run workflow` から
  手動実行も可能です
- ニュースソースの取得失敗やAPIエラーが発生した場合、ワークフローは失敗
  (赤色)として終了します。Actionsのログで原因を確認してください
