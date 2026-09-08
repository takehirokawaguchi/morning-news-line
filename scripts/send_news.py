#!/usr/bin/env python3
"""
自分用の朝刊ニュースを収集・要約し、LINEに配信するスクリプト。

- 技術系ニュース 3件(英語ソースは最大1件、残り2件以上は日本語ソース)
- 技術系以外のニュース 7件(英語ソースは最大3件、残り4件以上は日本語ソース)
  (世界情勢・金融経済・一般ニュースを広くカバー)
を Gemini API (gemini-2.5-flash、無料枠内) で日本語3〜4行に要約し、
LINE Messaging API の push message で配信する。

記事構成のルール(件数・言語比率)を変更したい場合は SELECTION_RULES を、
ニュースソースを変更・追加したい場合は SOURCES 以下の fetch_* 関数を編集する。
詳細は README.md を参照。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import feedparser
import requests
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

# --------------------------------------------------------------------------
# 基本設定
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("send_news")

JST = timezone(timedelta(hours=9))
HTTP_TIMEOUT = 10
HTTP_HEADERS = {
    "User-Agent": "morning-news-line/1.0 (+https://github.com/)"
}

GEMINI_MODEL = "gemini-2.5-flash"  # 無料枠で使える標準的なflash系モデル。変更したい場合はここを編集

CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "sent_urls.json",
)
CACHE_RETENTION_DAYS = 30  # 直近何日分のURLを重複排除の対象として保持するか

# 記事構成ルール。件数・英語比率を変えたい場合はここを編集する。
SELECTION_RULES = {
    "tech": {"total": 3, "max_english": 1},
    "nontech": {"total": 7, "max_english": 3},
}

# 技術系以外を広げるためのGoogle Newsキーワード検索の既定リスト。
# query/language(ja|en)/category(world|finance|general) を編集・追加・削除して調整できる。
#
# パブリックリポジトリに検索キーワード(=個人の興味関心)をそのまま載せたくない場合は、
# GitHub Secrets に GOOGLE_NEWS_KEYWORDS_JSON という名前で同じ形式のJSON配列を設定すると、
# 実行時にそちらが優先される(load_google_news_keywords() を参照)。
DEFAULT_GOOGLE_NEWS_KEYWORDS = [
    {"query": "国際情勢", "language": "ja", "category": "world"},
    {"query": "外交 安全保障", "language": "ja", "category": "world"},
    {"query": "紛争 停戦", "language": "ja", "category": "world"},
    {"query": "geopolitics", "language": "en", "category": "world"},
    {"query": "international relations", "language": "en", "category": "world"},
    {"query": "金融政策 中央銀行", "language": "ja", "category": "finance"},
    {"query": "株式市場", "language": "ja", "category": "finance"},
    {"query": "為替 金利", "language": "ja", "category": "finance"},
    {"query": "financial markets", "language": "en", "category": "finance"},
    {"query": "global economy", "language": "en", "category": "finance"},
    {"query": "国内ニュース", "language": "ja", "category": "general"},
    {"query": "社会問題", "language": "ja", "category": "general"},
    {"query": "政治", "language": "ja", "category": "general"},
    {"query": "科学 医療", "language": "ja", "category": "general"},
    {"query": "world news", "language": "en", "category": "general"},
]


# --------------------------------------------------------------------------
# データ構造
# --------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    url: str
    source: str          # 表示用のソース名 (例: "Hacker News")
    language: str         # "en" または "ja"
    category: str         # "tech" / "world" / "finance" / "general"
    snippet: str = ""     # 見出し以外に取得できた説明文(あれば)
    summary: str = field(default="", repr=False)


class NewsError(RuntimeError):
    """ニュース収集・要約・送信のいずれかで致命的なエラーが起きたことを示す。"""


# --------------------------------------------------------------------------
# URL正規化・重複排除キャッシュ
# --------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """比較用にトラッキングクエリ等を除去したURLを返す。"""
    try:
        parsed = urlparse(url)
        query = [
            (k, v)
            for k, v in parse_qsl(parsed.query)
            if not k.lower().startswith("utm_") and k.lower() not in ("ref", "ref_src")
        ]
        cleaned = parsed._replace(query=urlencode(query), fragment="")
        return urlunparse(cleaned)
    except Exception:
        return url


def load_cache() -> dict:
    """{normalized_url: iso_date} の辞書を読み込み、期限切れのエントリを除去する。"""
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("キャッシュファイルの読み込みに失敗したため空として扱います: %s", e)
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_RETENTION_DAYS)
    fresh = {}
    for url, date_str in raw.items():
        try:
            dt = datetime.fromisoformat(date_str)
        except ValueError:
            continue
        if dt >= cutoff:
            fresh[url] = date_str
    return fresh


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


# --------------------------------------------------------------------------
# ニュース取得
# --------------------------------------------------------------------------

def _get(url: str, params: dict | None = None) -> requests.Response:
    resp = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp


def fetch_rss(url: str, source: str, language: str, category: str, limit: int = 15) -> list[Article]:
    """汎用RSSフィード取得。取得に失敗した場合は空リストを返し、呼び出し元をブロックしない。"""
    try:
        resp = _get(url)
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        log.warning("RSS取得に失敗しました (source=%s): %s", source, e)
        return []

    articles = []
    for entry in parsed.entries[:limit]:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not title or not link:
            continue
        snippet = getattr(entry, "summary", "") or ""
        articles.append(
            Article(
                title=title,
                url=link,
                source=source,
                language=language,
                category=category,
                snippet=snippet[:500],
            )
        )
    return articles


def fetch_hackernews(limit: int = 8) -> list[Article]:
    """Hacker News Top Stories (英語・技術系)"""
    try:
        ids = _get("https://hacker-news.firebaseio.com/v0/topstories.json").json()
    except Exception as e:
        log.warning("Hacker News のトップ記事一覧取得に失敗しました: %s", e)
        return []

    articles = []
    for story_id in ids[: limit * 2]:
        if len(articles) >= limit:
            break
        try:
            item = _get(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json").json()
        except Exception as e:
            log.warning("Hacker News item=%s の取得に失敗しました: %s", story_id, e)
            continue
        if not item or item.get("type") != "story":
            continue
        title = item.get("title", "").strip()
        url = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
        if not title:
            continue
        articles.append(
            Article(
                title=title,
                url=url,
                source="Hacker News",
                language="en",
                category="tech",
            )
        )
    return articles


def fetch_qiita_trend(limit: int = 10) -> list[Article]:
    """Qiita: 直近数日で「いいね」の多い記事を簡易的な「トレンド」として取得(日本語・技術系)"""
    since = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%d")
    try:
        resp = _get(
            "https://qiita.com/api/v2/items",
            params={"page": 1, "per_page": 100, "query": f"created:>{since}"},
        )
        items = resp.json()
    except Exception as e:
        log.warning("Qiita トレンド取得に失敗しました: %s", e)
        return []

    items.sort(key=lambda it: it.get("likes_count", 0), reverse=True)
    articles = []
    for it in items[:limit]:
        title = it.get("title", "").strip()
        url = it.get("url", "")
        if not title or not url:
            continue
        articles.append(
            Article(title=title, url=url, source="Qiita", language="ja", category="tech")
        )
    return articles


def fetch_zenn_trend(limit: int = 10) -> list[Article]:
    """Zenn: デイリートレンド記事一覧(日本語・技術系)"""
    try:
        resp = _get("https://zenn.dev/api/articles", params={"order": "daily", "count": limit})
        data = resp.json()
    except Exception as e:
        log.warning("Zenn トレンド取得に失敗しました: %s", e)
        return []

    articles = []
    for it in data.get("articles", [])[:limit]:
        title = it.get("title", "").strip()
        path = it.get("path", "")
        if not title or not path:
            continue
        url = f"https://zenn.dev{path}"
        articles.append(
            Article(title=title, url=url, source="Zenn", language="ja", category="tech")
        )
    return articles


def load_google_news_keywords() -> list[dict]:
    """
    Google Newsキーワード検索に使うキーワード設定を返す。

    パブリックリポジトリのコードに検索キーワード(=個人の興味関心)を直接
    書きたくない場合、環境変数 GOOGLE_NEWS_KEYWORDS_JSON (GitHub Secretsから
    注入) に DEFAULT_GOOGLE_NEWS_KEYWORDS と同じ形式のJSON配列を設定すれば
    そちらが優先される。未設定/解析失敗時は DEFAULT_GOOGLE_NEWS_KEYWORDS を使う。
    """
    raw = os.environ.get("GOOGLE_NEWS_KEYWORDS_JSON")
    if not raw:
        return DEFAULT_GOOGLE_NEWS_KEYWORDS
    try:
        keywords = json.loads(raw)
        for kw in keywords:
            if not all(k in kw for k in ("query", "language", "category")):
                raise ValueError("各キーワードには query/language/category が必要です")
        log.info("GOOGLE_NEWS_KEYWORDS_JSON からキーワード設定を読み込みました (%d件)", len(keywords))
        return keywords
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        log.warning(
            "GOOGLE_NEWS_KEYWORDS_JSON の解析に失敗したため既定のキーワードを使用します: %s", e
        )
        return DEFAULT_GOOGLE_NEWS_KEYWORDS


def _google_news_locale(language: str) -> tuple[str, str, str]:
    """言語コードから Google News の hl/gl/ceid パラメータを決める。"""
    if language == "en":
        return "en-US", "US", "US:en"
    return "ja", "JP", "JP:ja"


def fetch_google_news_search(
    query: str, language: str, category: str, limit: int = 5
) -> list[Article]:
    """
    Google News のキーワード検索RSS(無料・APIキー不要)。ロイター・共同・日経・AP・
    Bloombergなど多数の媒体を横断的に拾える。`when:1d` を付与し直近24時間に絞る。
    """
    hl, gl, ceid = _google_news_locale(language)
    try:
        resp = _get(
            "https://news.google.com/rss/search",
            params={"q": f"{query} when:1d", "hl": hl, "gl": gl, "ceid": ceid},
        )
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        log.warning("Google News検索(query=%s)の取得に失敗しました: %s", query, e)
        return []

    articles = []
    for entry in parsed.entries[:limit]:
        raw_title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not raw_title or not link:
            continue

        # Google Newsのtitleは "記事タイトル - 媒体名" の形式。
        # <source>要素があればそちらを優先し、媒体名を分離する。
        source_tag = getattr(entry, "source", None)
        if source_tag is not None and getattr(source_tag, "title", ""):
            publisher = source_tag.title.strip()
            title = raw_title
            if title.endswith(f" - {publisher}"):
                title = title[: -(len(publisher) + 3)].strip()
        elif " - " in raw_title:
            title, publisher = raw_title.rsplit(" - ", 1)
            title, publisher = title.strip(), publisher.strip()
        else:
            title, publisher = raw_title, "Google News"

        articles.append(
            Article(
                title=title,
                url=link,
                source=publisher,
                language=language,
                category=category,
            )
        )
    return articles


def collect_source_pools() -> tuple[list[Article], list[Article], list[Article], list[Article]]:
    """(技術系:日本語, 技術系:英語, 技術系以外:日本語, 技術系以外:英語) のプールを返す。"""

    tech_ja = fetch_qiita_trend() + fetch_zenn_trend()
    tech_en = fetch_hackernews()

    keywords = load_google_news_keywords()
    google_news_ja = [
        article
        for kw in keywords
        if kw["language"] == "ja"
        for article in fetch_google_news_search(kw["query"], kw["language"], kw["category"])
    ]
    google_news_en = [
        article
        for kw in keywords
        if kw["language"] == "en"
        for article in fetch_google_news_search(kw["query"], kw["language"], kw["category"])
    ]

    nontech_ja = google_news_ja
    nontech_en = (
        fetch_rss("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC News", "en", "world")
        + fetch_rss("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC News(Business)", "en", "finance")
        + google_news_en
    )

    return tech_ja, tech_en, nontech_ja, nontech_en


# --------------------------------------------------------------------------
# 記事選定
# --------------------------------------------------------------------------

def dedupe(articles: list[Article], seen_urls: set[str]) -> list[Article]:
    """記事リストから、既にキャッシュ済み/このリスト内で重複するURLを取り除く。"""
    out = []
    local_seen = set()
    for a in articles:
        key = normalize_url(a.url)
        if key in seen_urls or key in local_seen:
            continue
        local_seen.add(key)
        out.append(a)
    return out


def select_articles(japanese: list[Article], english: list[Article], total: int, max_english: int) -> list[Article]:
    """
    日本語ソースを優先しつつ、英語ソースを max_english 件まで含めて
    合計 total 件を選ぶ。日本語記事が不足する場合は英語で埋め合わせる
    (それでも足りない場合は取得できた分だけを返す)。
    """
    en_count = min(max_english, len(english))
    ja_needed = total - en_count

    if len(japanese) < ja_needed:
        shortfall = ja_needed - len(japanese)
        en_count = min(en_count + shortfall, len(english))
        ja_needed = total - en_count

    selected = japanese[:ja_needed] + english[:en_count]

    if len(selected) < total:
        log.warning(
            "十分な記事数を確保できませんでした (取得できた件数=%d / 目標=%d)",
            len(selected),
            total,
        )
    return selected[:total]


# --------------------------------------------------------------------------
# 要約 (Gemini API)
# --------------------------------------------------------------------------

BASE_SYSTEM_PROMPT = """\
あなたはニュース要約アシスタントです。与えられた記事のタイトル・概要をもとに、\
日本語で3〜4行程度の簡潔な要約を作成してください。

- 元記事が英語であっても、要約は必ず自然な日本語で書いてください。
- 与えられた情報の範囲で要約し、記載のない事実を推測で補わないでください。
- 誇張表現や個人的な意見・感想は含めず、事実を淡々と伝えてください。
- 出典表記(「出典:」等)は書かないでください。呼び出し側で別途付与します。
- 要約本文のみを出力し、前置きや後書き、見出しの繰り返しは書かないでください。
"""

WORLD_AFFAIRS_ADDENDUM = """\

この記事は国際情勢に関するニュースです。特定の国・勢力・立場に偏らない、\
事実ベースでフラットな要約にしてください。単一の主張を断定的に伝えるのではなく、\
対立する見方や複数の立場が存在する場合は、その旨も簡潔に触れてください。
"""


def build_system_prompt(category: str) -> str:
    if category == "world":
        return BASE_SYSTEM_PROMPT + WORLD_AFFAIRS_ADDENDUM
    return BASE_SYSTEM_PROMPT


def summarize_article(client: genai.Client, article: Article) -> str:
    user_content = f"タイトル: {article.title}\n"
    if article.snippet:
        user_content += f"概要: {article.snippet}\n"

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_content,
        config=genai_types.GenerateContentConfig(
            system_instruction=build_system_prompt(article.category),
            temperature=0.3,
            max_output_tokens=400,
        ),
    )
    return (response.text or "").strip()


def summarize_all(client: genai.Client, articles: list[Article]) -> list[Article]:
    ok, failed = [], []
    for article in articles:
        try:
            article.summary = summarize_article(client, article)
            ok.append(article)
        except genai_errors.APIError as e:
            if e.code == 429:
                # 無料枠のレート制限/クォータ超過。残りの記事に対して同じリクエストを
                # 繰り返しても無駄なので、即座に実行全体を中断する。
                raise NewsError(
                    "Gemini APIのレート制限(無料枠の上限)に達しました。"
                    "しばらく待つか、Google AI Studioで利用状況を確認してください。"
                ) from e
            log.error(
                "要約に失敗したため、この記事はスキップします (source=%s, code=%s): %s",
                article.source, e.code, e.message,
            )
            failed.append(article)

    if not ok:
        raise NewsError("すべての記事の要約に失敗しました。Gemini APIの状態を確認してください。")
    if failed:
        log.warning("%d件の記事で要約に失敗しました(送信からは除外)", len(failed))
    return ok


# --------------------------------------------------------------------------
# LINEメッセージ整形・送信
# --------------------------------------------------------------------------

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_MAX_MESSAGES_PER_CALL = 5
LINE_MAX_CHARS = 4900  # LINEのテキストメッセージ上限(5000文字)に対する安全マージン


def language_label(language: str) -> str:
    return "英語" if language == "en" else "日本語"


def format_article_block(index: int, article: Article) -> str:
    return (
        f"{index}. {article.title}\n"
        f"{article.summary}\n"
        f"出典: {article.source}({language_label(article.language)}記事)\n"
        f"{article.url}"
    )


def build_section_messages(header: str, articles: list[Article]) -> list[str]:
    """1セクション分の記事を、LINEの文字数上限に収まるようメッセージ文字列のリストに分割する。"""
    messages = []
    current = header
    index = 1
    for article in articles:
        block = format_article_block(index, article)
        candidate = current + "\n\n" + block
        if len(candidate) > LINE_MAX_CHARS and current != header:
            messages.append(current)
            current = header + "(続き)\n\n" + block
        else:
            current = candidate
        index += 1
    messages.append(current)
    return messages


def build_line_messages(tech_articles: list[Article], nontech_articles: list[Article]) -> list[dict]:
    today = datetime.now(JST).strftime("%Y-%m-%d")
    tech_header = f"🗞️ 朝刊ニュース - 技術系 ({today})"
    nontech_header = f"🌍 朝刊ニュース - 一般・世界情勢・金融 ({today})"

    texts = build_section_messages(tech_header, tech_articles) + build_section_messages(
        nontech_header, nontech_articles
    )
    return [{"type": "text", "text": t} for t in texts]


def send_line_push(access_token: str, user_id: str, messages: list[dict]) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    for i in range(0, len(messages), LINE_MAX_MESSAGES_PER_CALL):
        batch = messages[i : i + LINE_MAX_MESSAGES_PER_CALL]
        payload = {"to": user_id, "messages": batch}
        resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            # LINEのエラーレスポンス本文にはアクセストークン等は含まれないため、
            # デバッグ用にステータスコードのみログ出力する(本文はユーザー情報を含み得るため出力しない)。
            raise NewsError(f"LINE push message API がエラーを返しました (status={resp.status_code})")


# --------------------------------------------------------------------------
# メイン処理
# --------------------------------------------------------------------------

def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise NewsError(f"環境変数 {name} が設定されていません。")
    return value


def main() -> int:
    try:
        line_token = require_env("LINE_CHANNEL_ACCESS_TOKEN")
        line_user_id = require_env("LINE_USER_ID")
        gemini_api_key = require_env("GEMINI_API_KEY")

        cache = load_cache()
        seen_urls = set(cache.keys())

        log.info("ニュースソースから記事を取得しています...")
        tech_ja, tech_en, nontech_ja, nontech_en = collect_source_pools()

        tech_ja = dedupe(tech_ja, seen_urls)
        tech_en = dedupe(tech_en, seen_urls)
        nontech_ja = dedupe(nontech_ja, seen_urls)
        nontech_en = dedupe(nontech_en, seen_urls)

        log.info(
            "取得件数(重複排除後): 技術系 日本語=%d 英語=%d / 技術系以外 日本語=%d 英語=%d",
            len(tech_ja), len(tech_en), len(nontech_ja), len(nontech_en),
        )

        tech_rule = SELECTION_RULES["tech"]
        nontech_rule = SELECTION_RULES["nontech"]

        tech_selected = select_articles(tech_ja, tech_en, tech_rule["total"], tech_rule["max_english"])
        nontech_selected = select_articles(nontech_ja, nontech_en, nontech_rule["total"], nontech_rule["max_english"])

        if not tech_selected or not nontech_selected:
            raise NewsError(
                "配信に必要な記事を十分に取得できませんでした"
                f"(技術系={len(tech_selected)}件, 技術系以外={len(nontech_selected)}件)。"
                "ニュースソースの状態を確認してください。"
            )

        log.info(
            "選定件数: 技術系=%d件, 技術系以外=%d件", len(tech_selected), len(nontech_selected)
        )

        client = genai.Client(api_key=gemini_api_key)
        log.info("Gemini APIで要約を生成しています...")
        tech_summarized = summarize_all(client, tech_selected)
        nontech_summarized = summarize_all(client, nontech_selected)

        messages = build_line_messages(tech_summarized, nontech_summarized)

        log.info("LINEへメッセージを送信しています (メッセージ数=%d)...", len(messages))
        send_line_push(line_token, line_user_id, messages)
        log.info("LINEへの送信が完了しました。")

        now_iso = datetime.now(timezone.utc).isoformat()
        for article in tech_summarized + nontech_summarized:
            cache[normalize_url(article.url)] = now_iso
        save_cache(cache)
        log.info("配信済みURLキャッシュを更新しました (保持件数=%d)", len(cache))

        return 0

    except NewsError as e:
        log.error("処理を中断しました: %s", e)
        return 1
    except Exception as e:  # 想定外のエラーもワークフローを失敗させる
        log.error("予期しないエラーが発生しました: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
