#!/usr/bin/env python3
"""Generate, validate, archive and optionally push the daily market digest."""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import hmac
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "docs" / "data"
BEIJING = ZoneInfo("Asia/Shanghai")
ENGINE = "zero-api-rules-v1"
LOOKBACK_HOURS = 36
FALLBACK_LOOKBACK_HOURS = 24 * 7
QUOTA = 10
MAX_ITEMS = 40
REGIONS = {"A股", "港股", "美股", "全球宏观"}
ASSETS = {"股票", "基金", "ETF"}
TONES = {"偏积极", "偏谨慎", "中性", "混合"}
BLOCKED_HOSTS = {"reddit.com", "www.reddit.com", "quora.com", "www.quora.com", "wikipedia.org", "www.wikipedia.org"}
USER_AGENT = "stock-daily-dashboard/1.0 (+https://github.com/junpeng957-coder/stock-daily-dashboard)"

RSS_SOURCES = [
    ("美联储", "https://www.federalreserve.gov/feeds/press_all.xml", "全球宏观", True),
    ("美国证监会", "https://www.sec.gov/news/pressreleases.rss", "美股", True),
    ("香港交易所", "https://www.hkex.com.hk/Services/RSS-Feeds/News-Releases?sc_lang=zh-HK", "港股", True),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "美股", False),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "美股", False),
]

HTML_SOURCES = [
    ("中国证监会", "https://www.csrc.gov.cn/csrc/xwfb/index.shtml", "A股", True),
    ("上海证券交易所", "https://www.sse.com.cn/aboutus/mediacenter/hotandd/", "A股", True),
    ("深圳证券交易所", "https://www.szse.cn/aboutus/trends/news/", "A股", True),
    ("天天基金", "https://fund.eastmoney.com/a/cjjyw_1.html", "A股", False),
]

EASTMONEY_COLUMNS = [
    ("东方财富·A股", 416, "A股"),
    ("东方财富·港股", 797, "港股"),
]

QUOTA_KEYS = ("A股", "港股", "股票", "基金", "ETF")

TOPICS = [
    (("etf", "mutual fund", "fund manager", "fund flow", "asset manager", "基金"), "基金与ETF", "可能影响基金申赎、资金配置和相关指数产品表现。", ["基金", "ETF"]),
    (("earnings", "revenue", "profit", "guidance", "results", "业绩", "营收", "利润", "财报"), "公司业绩", "业绩与指引会改变市场对公司盈利和估值的预期。", ["股票", "基金"]),
    (("ipo", "listing", "listed", "上市", "发行"), "IPO与融资", "融资与上市安排会影响市场供给、估值参照和相关板块情绪。", ["股票", "基金"]),
    (("merger", "acquisition", "takeover", "并购", "重组", "收购"), "并购重组", "交易条款和整合预期可能影响相关公司估值与行业格局。", ["股票", "基金"]),
    (("buyback", "dividend", "回购", "分红", "增持", "减持"), "股东回报", "资本运作会直接影响流通供给、股东回报和市场预期。", ["股票", "基金"]),
    (("federal reserve", "fed ", "interest rate", "monetary", "inflation", "cpi", "pce", "payroll", "jobs", "央行", "利率", "通胀", "降息", "加息"), "利率与通胀", "利率和通胀预期会影响权益估值、美元流动性及成长板块定价。", ["股票", "基金", "ETF"]),
    (("sec ", "regulation", "rule", "enforcement", "suspension", "监管", "规则", "处罚", "立案", "退市"), "监管政策", "监管变化可能影响上市公司合规成本、交易制度和风险偏好。", ["股票", "基金", "ETF"]),
    (("tariff", "sanction", "trade", "关税", "制裁", "贸易"), "贸易政策", "贸易政策变化可能传导至企业成本、供应链和跨市场风险偏好。", ["股票", "基金", "ETF"]),
    (("stock", "share", "market", "nasdaq", "s&p", "dow", "option", "指数", "股市", "股票", "成交", "期权"), "市场与交易", "市场结构或交易变化可能影响流动性、波动率及相关资产定价。", ["股票", "基金", "ETF"]),
]

RELEVANT_TERMS = tuple(term for terms, *_ in TOPICS for term in terms)
EXCLUDED_TERMS = ("crypto", "bitcoin", "ethereum", "mortgage", "real estate", "personal finance", "social security", "donation", "donations", "charity", "funding round", "fund round", "work authorization", "退休顾问", "房贷", "加密货币")
POSITIVE_TERMS = ("beat", "beats", "gain", "gains", "rise", "rises", "record", "approval", "buyback", "dividend", "stimulus", "增长", "上涨", "回购", "增持", "降息", "获批", "创新高")
NEGATIVE_TERMS = ("miss", "misses", "fall", "falls", "drop", "drops", "warning", "probe", "charge", "enforcement", "tariff", "downgrade", "亏损", "下跌", "减持", "处罚", "调查", "风险", "退市")


def now_beijing() -> datetime:
    return datetime.now(BEIJING)


def parse_watchlist(raw: str) -> list[str]:
    if not raw.strip():
        return []
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("WATCHLIST_JSON 必须是 JSON 数组")
    result = []
    for entry in value:
        if isinstance(entry, str):
            label = entry.strip()
        elif isinstance(entry, dict):
            label = " ".join(str(entry.get(key, "")).strip() for key in ("symbol", "name")).strip()
        else:
            raise ValueError("WATCHLIST_JSON 每项必须是字符串或含 symbol/name 的对象")
        if label:
            result.append(label[:80])
    return result[:100]


def request_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 180) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def fetch_text(url: str, timeout: int = 25) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml, application/xml, text/html, */*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(3_000_001)
        if len(data) > 3_000_000:
            raise ValueError("来源页面超过 3 MB")
        charset = response.headers.get_content_charset()
        if not charset:
            head = data[:2000].decode("ascii", "ignore")
            match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", head, re.I)
            charset = match.group(1) if match else "utf-8"
        if charset.lower() in {"gb2312", "gbk"}:
            charset = "gb18030"
        return data.decode(charset, "replace")


def eastmoney_items(name: str, column: int, region: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "client": "web",
            "biz": "web_news_col",
            "column": column,
            "order": 1,
            "needInteractData": 0,
            "page_index": 1,
            "page_size": 50,
            "req_trace": f"dashboard-{int(time.time())}",
            "fields": "code,showTime,title,mediaName,summary,uniqueUrl,Np_dst",
            "types": "1,20",
        }
    )
    payload = json.loads(fetch_text(f"https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?{query}"))
    result = []
    for item in payload.get("data", {}).get("list", []):
        code = str(item.get("code", "")).strip()
        title = clean_text(str(item.get("title", "")))
        published = str(item.get("showTime", "")).strip()
        if not (code and title and published):
            continue
        result.append(
            {
                "source_name": clean_text(str(item.get("mediaName", ""))) or name,
                "source_url": f"https://finance.eastmoney.com/a/{code}.html",
                "source_title": title,
                "source_summary": clean_text(str(item.get("summary", ""))),
                "published_at": datetime.fromisoformat(published).replace(tzinfo=BEIJING),
                "region": region,
                "official": False,
            }
        )
    return result


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_feed_date(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING)


def feed_items(name: str, url: str, region: str, official: bool) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(fetch_text(url).lstrip("\ufeff"))
    result = []
    for node in root.findall(".//item"):
        fields = {child.tag.rsplit("}", 1)[-1]: clean_text(child.text or "") for child in node}
        if fields.get("title") and fields.get("link") and (fields.get("pubDate") or fields.get("date")):
            result.append(
                {
                    "source_name": name,
                    "source_url": fields["link"],
                    "source_title": fields["title"],
                    "source_summary": fields.get("description", ""),
                    "published_at": parse_feed_date(fields.get("pubDate") or fields["date"]),
                    "region": region,
                    "official": official,
                }
            )
    return result


def nearest_page_date(page: str, position: int, current: datetime) -> datetime | None:
    start, end = max(0, position - 500), min(len(page), position + 500)
    candidates = []
    for match in re.finditer(r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?|(?<!\d)(\d{1,2})[-/.](\d{1,2})(?!\d)", page[start:end]):
        try:
            if match.group(1):
                parsed = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), 12, tzinfo=BEIJING)
            else:
                parsed = datetime(current.year, int(match.group(4)), int(match.group(5)), 12, tzinfo=BEIJING)
                if parsed > current + timedelta(days=2):
                    parsed = parsed.replace(year=current.year - 1)
            candidates.append((abs(start + match.start() - position), parsed))
        except ValueError:
            continue
    return min(candidates, default=(0, None))[1]


def html_items(name: str, url: str, region: str, official: bool, current: datetime) -> list[dict[str, Any]]:
    page = fetch_text(url)
    result = []
    for match in re.finditer(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page, re.I | re.S):
        title = clean_text(match.group(2))
        published = nearest_page_date(page, match.start(), current)
        if published and 8 <= len(title) <= 180:
            result.append(
                {
                    "source_name": name,
                    "source_url": urllib.parse.urljoin(url, html.unescape(match.group(1))),
                    "source_title": title,
                    "source_summary": title,
                    "published_at": published,
                    "region": region,
                    "official": official,
                }
            )
    return result


def topic_for(text: str) -> tuple[str, str, list[str]]:
    lowered = text.lower()
    for terms, label, why, assets in TOPICS:
        if any(has_term(lowered, term) for term in terms):
            return label, why, assets
    return "资本市场动态", "该信息可能影响市场预期、风险偏好或相关资产定价。", ["股票", "基金"]


def market_tone(text: str) -> str:
    lowered = text.lower()
    positive = any(has_term(lowered, term) for term in POSITIVE_TERMS)
    negative = any(has_term(lowered, term) for term in NEGATIVE_TERMS)
    if positive and negative:
        return "混合"
    if positive:
        return "偏积极"
    if negative:
        return "偏谨慎"
    return "中性"


def is_relevant(text: str) -> bool:
    lowered = text.lower()
    return any(has_term(lowered, term) for term in RELEVANT_TERMS) and not any(has_term(lowered, term) for term in EXCLUDED_TERMS)


def has_term(lowered: str, term: str) -> bool:
    term = term.lower().strip()
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in lowered
    return bool(re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", lowered))


def zh_item(item: dict[str, Any], watchlist: list[str], current: datetime) -> dict[str, Any] | None:
    text = f"{item['source_title']} {item['source_summary']}"
    if not is_relevant(text) or not (current - timedelta(hours=FALLBACK_LOOKBACK_HOURS) <= item["published_at"] <= current + timedelta(hours=2)):
        return None
    topic, why, assets = topic_for(text)
    original_title = clean_text(item["source_title"])
    chinese = bool(re.search(r"[\u4e00-\u9fff]", original_title))
    title = original_title if chinese else f"{item['source_name']}：{topic}｜{original_title}"
    description = clean_text(item["source_summary"])
    if chinese:
        summary = description if description != original_title else f"{item['source_name']}发布与“{topic}”相关的新信息。"
    else:
        summary = f"英文摘要：{description or original_title}"
    matched = [entry for entry in watchlist if any(part.lower() in text.lower() for part in entry.split() if len(part) >= 2)]
    importance = 2 + int(item["official"]) + int(topic in {"利率与通胀", "监管政策", "公司业绩"})
    return {
        "title": title[:160],
        "summary": summary[:500],
        "why_it_matters": why,
        "region": item["region"],
        "asset_types": assets,
        "published_at": item["published_at"].isoformat(timespec="minutes"),
        "importance": min(5, importance),
        "market_tone": market_tone(text),
        "source_name": item["source_name"],
        "source_url": item["source_url"],
        "official": item["official"],
        "watchlist_match": bool(matched),
        "watchlist_symbols": matched,
        "topic": topic,
    }


def collect_digest(watchlist: list[str], current: datetime) -> dict[str, Any]:
    collected: list[dict[str, Any]] = []
    for source in RSS_SOURCES:
        try:
            collected.extend(feed_items(*source))
        except Exception as exc:
            print(f"来源读取失败：{source[0]}：{exc}", file=sys.stderr)
    for source in HTML_SOURCES:
        try:
            collected.extend(html_items(*source, current))
        except Exception as exc:
            print(f"来源读取失败：{source[0]}：{exc}", file=sys.stderr)
    for source in EASTMONEY_COLUMNS:
        try:
            collected.extend(eastmoney_items(*source))
        except Exception as exc:
            print(f"来源读取失败：{source[0]}：{exc}", file=sys.stderr)

    items = [candidate for raw in collected if (candidate := zh_item(raw, watchlist, current))]
    items.sort(key=lambda item: (item["watchlist_match"], item["importance"], item["published_at"]), reverse=True)
    candidates: list[dict[str, Any]] = []
    for item in items:
        key = normalize_title(item["title"])
        entities = set(re.findall(r"\b[A-Z][A-Z0-9.]{1,7}\b", item["title"])) - {"IPO", "ETF", "SEC", "FED", "CNBC"}
        # ponytail: O(n²) is simpler and bounded to a few dozen feed items.
        if any(
            difflib.SequenceMatcher(None, key, normalize_title(saved["title"])).ratio() > 0.84
            or (
                entities
                and entities & (set(re.findall(r"\b[A-Z][A-Z0-9.]{1,7}\b", saved["title"])) - {"IPO", "ETF", "SEC", "FED", "CNBC"})
                and item["why_it_matters"] == saved["why_it_matters"]
            )
            for saved in candidates
        ):
            continue
        candidates.append(item)

    def matches(item: dict[str, Any], key: str) -> bool:
        return item["region"] == key if key in REGIONS else key in item["asset_types"]

    unique: list[dict[str, Any]] = []
    for key in QUOTA_KEYS:
        for item in candidates:
            if sum(matches(saved, key) for saved in unique) >= QUOTA:
                break
            if item not in unique and matches(item, key):
                unique.append(item)
                if len(unique) >= MAX_ITEMS:
                    break
    if not unique:
        raise ValueError("最近 7 天没有筛选到可靠的股票或基金资讯，不覆盖上一期看板")

    coverage = {key: min(QUOTA, sum(matches(item, key) for item in unique)) for key in QUOTA_KEYS}

    topic_counts: dict[str, int] = {}
    for item in unique:
        topic_counts[item["topic"]] = topic_counts.get(item["topic"], 0) + 1
    top_topics = "、".join(name for name, _ in sorted(topic_counts.items(), key=lambda pair: pair[1], reverse=True)[:3])
    official_count = sum(item["official"] for item in unique)
    for item in unique:
        item.pop("topic", None)
    return {
        "headline": f"{current:%m月%d日} 全球股市与基金晨报",
        "market_summary": f"优先检索最近 {LOOKBACK_HOURS} 小时，不足类别回溯至 7 天，共筛选 {len(unique)} 条去重资讯；A股、港股、股票、基金、ETF 五个板块各精选最多 {QUOTA} 条，其中 {official_count} 条来自监管机构、交易所或央行。重点集中在{top_topics}。一条资讯可能进入多个板块，不构成买卖建议。",
        "items": unique,
        "coverage": coverage,
    }


def parse_published_at(value: str, current: datetime) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed.astimezone(BEIJING)


def normalize_title(value: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value).lower()


def validate_digest(raw: dict[str, Any], current: datetime, source_hosts: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ValueError("日报结构无效")
    headline = str(raw.get("headline", "")).strip()[:80]
    summary = str(raw.get("market_summary", "")).strip()[:800]
    if not headline or not summary:
        raise ValueError("日报缺少标题或市场总览")

    cutoff = current - timedelta(hours=FALLBACK_LOOKBACK_HOURS)
    future_limit = current + timedelta(hours=2)
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()
    items = []
    for item in raw["items"]:
        try:
            url = str(item["source_url"]).strip()
            parsed_url = urllib.parse.urlparse(url)
            host = (parsed_url.hostname or "").lower().removeprefix("www.")
            published = parse_published_at(str(item["published_at"]), current)
            title_key = normalize_title(str(item["title"]))
            if parsed_url.scheme != "https" or not host or host in BLOCKED_HOSTS:
                continue
            if source_hosts and host not in source_hosts:
                continue
            if not (cutoff <= published <= future_limit):
                continue
            if not title_key or title_key in seen_titles or url in seen_urls:
                continue
            region = str(item["region"])
            assets = [value for value in item["asset_types"] if value in ASSETS]
            tone = str(item["market_tone"])
            if region not in REGIONS or not assets or tone not in TONES:
                continue
            clean = {
                "id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:12],
                "title": str(item["title"]).strip()[:160],
                "summary": str(item["summary"]).strip()[:500],
                "why_it_matters": str(item["why_it_matters"]).strip()[:300],
                "region": region,
                "asset_types": assets,
                "published_at": published.isoformat(timespec="minutes"),
                "importance": max(1, min(5, int(item["importance"]))),
                "market_tone": tone,
                "source_name": str(item["source_name"]).strip()[:80],
                "source_url": url,
                "official": bool(item["official"]),
                "watchlist_match": bool(item.get("watchlist_match")),
                "watchlist_symbols": [str(value)[:80] for value in item.get("watchlist_symbols", [])[:20]],
            }
            if not all(clean[key] for key in ("title", "summary", "why_it_matters", "source_name")):
                continue
            seen_titles.add(title_key)
            seen_urls.add(url)
            items.append(clean)
        except (KeyError, TypeError, ValueError):
            continue

    if not items:
        raise ValueError("筛选后没有可靠资讯，不覆盖上一期看板")
    items.sort(key=lambda item: (item["watchlist_match"], item["importance"], item["published_at"]), reverse=True)
    coverage = {
        key: min(QUOTA, sum(item["region"] == key if key in REGIONS else key in item["asset_types"] for item in items[:MAX_ITEMS]))
        for key in QUOTA_KEYS
    }
    return {"headline": headline, "market_summary": summary, "items": items[:MAX_ITEMS], "coverage": coverage}


def public_digest(validated: dict[str, Any], current: datetime) -> dict[str, Any]:
    items = []
    for item in validated["items"]:
        public_item = {key: value for key, value in item.items() if key not in {"watchlist_match", "watchlist_symbols"}}
        items.append(public_item)
    return {
        "version": 1,
        "date": current.date().isoformat(),
        "generated_at": current.isoformat(timespec="minutes"),
        "timezone": "Asia/Shanghai",
        "model": ENGINE,
        "headline": validated["headline"],
        "market_summary": validated["market_summary"],
        "coverage": validated.get("coverage", {}),
        "items": items,
        "disclaimer": "自动汇总，仅供信息参考，不构成任何投资建议。",
    }


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def save_digest(digest: dict[str, Any], current: datetime) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = current.date() - timedelta(days=29)
    for path in DATA_DIR.glob("????-??-??.json"):
        try:
            if datetime.strptime(path.stem, "%Y-%m-%d").date() < cutoff:
                path.unlink()
        except ValueError:
            continue
    dated_path = DATA_DIR / f"{digest['date']}.json"
    atomic_write_json(dated_path, digest)
    atomic_write_json(DATA_DIR / "latest.json", digest)
    archive = sorted((path.stem for path in DATA_DIR.glob("????-??-??.json")), reverse=True)
    atomic_write_json(DATA_DIR / "archive.json", {"dates": archive[:30]})
    (ROOT / "docs" / "mobile.html").write_text(render_mobile_html(digest), encoding="utf-8")


def render_mobile_html(digest: dict[str, Any]) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    coverage = digest.get("coverage", {})
    chips = "".join(f'<a href="#{esc(key)}">{esc(key)} {int(coverage.get(key, 0))}</a>' for key in QUOTA_KEYS)
    def card_html(item: dict[str, Any]) -> str:
        tags = " ".join(esc(value) for value in [item["region"], *item["asset_types"], item["market_tone"]])
        return (
            f'<article><small>{tags} · {esc(item["published_at"][:16].replace("T", " "))}</small>'
            f'<h3>{esc(item["title"])}</h3><p>{esc(item["summary"])}</p>'
            f'<p class="why"><b>值得关注：</b>{esc(item["why_it_matters"])}</p>'
            f'<a class="source" href="{esc(item["source_url"])}">原文 · {esc(item["source_name"])}</a></article>'
        )
    sections = []
    for key in QUOTA_KEYS:
        matched = [item for item in digest["items"] if item["region"] == key or key in item["asset_types"]][:QUOTA]
        sections.append(f'<section id="{esc(key)}"><h2>{esc(key)} · {len(matched)} 条</h2>{"".join(card_html(item) for item in matched)}</section>')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{esc(digest["headline"])}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#fff8ef;color:#17131f;font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}main{{max-width:760px;margin:auto;padding:20px 14px 48px}}header{{background:#ffcf3f;border:2px solid #17131f;border-radius:22px;padding:20px;box-shadow:4px 5px 0 #17131f}}h1{{font-size:28px;line-height:1.18;margin:5px 0 12px}}header p{{margin:8px 0}}nav{{display:flex;gap:8px;overflow:auto;padding:16px 2px 10px;position:sticky;top:0;background:#fff8ef}}nav a{{white-space:nowrap;color:#17131f;text-decoration:none;background:#92e7ff;border:2px solid #17131f;border-radius:999px;padding:7px 11px;font-weight:750}}section{{scroll-margin-top:70px}}section>h2{{font-size:23px;margin:24px 3px 8px}}article{{background:white;border:2px solid #17131f;border-radius:19px;padding:17px;margin:12px 0;box-shadow:3px 4px 0 #17131f}}article:nth-of-type(3n+1){{background:#e9dcff}}article:nth-of-type(3n+2){{background:#d8ff72}}small{{font-weight:750}}h3{{font-size:19px;line-height:1.4;margin:8px 0}}p{{margin:7px 0}}.why{{padding:10px;background:#ffffffa8;border-radius:12px}}.source{{display:inline-block;margin-top:7px;color:#17131f;font-weight:800}}footer{{text-align:center;color:#655d6b;margin-top:22px}}@media(max-width:380px){{h1{{font-size:24px}}article{{padding:14px}}}}
</style></head><body><main><header><small>{esc(digest["date"])} · {len(digest["items"])} 条去重资讯</small><h1>{esc(digest["headline"])}</h1><p>{esc(digest["market_summary"])}</p></header><nav>{chips}</nav>{''.join(sections)}<footer>{esc(digest["disclaimer"])}<br>更新于 {esc(digest["generated_at"])}</footer></main></body></html>'''


def dashboard_url() -> str:
    explicit = os.getenv("DASHBOARD_URL", "").strip()
    if explicit:
        return explicit.rstrip("/") + "/"
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in repository:
        owner, name = repository.split("/", 1)
        return f"https://{owner}.github.io/{name}/"
    return ""


def mobile_dashboard_url(digest: dict[str, Any]) -> str:
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if "/" not in repository:
        return ""
    return f"https://cdn.jsdelivr.net/gh/{repository}@main/docs/mobile.html?v={digest['date'].replace('-', '')}"


def sign_feishu(payload: dict[str, Any], secret: str) -> None:
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    payload["timestamp"] = timestamp
    payload["sign"] = base64.b64encode(digest).decode("utf-8")


def feishu_card(digest: dict[str, Any], private_items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    private_by_id = {item["id"]: item for item in private_items or []}
    lines = []
    for index, item in enumerate(digest["items"][:6], 1):
        private = private_by_id.get(item["id"], {})
        related = " · 自选相关" if private.get("watchlist_match") else ""
        lines.append(
            f"**{index}. {item['title']}**\n{item['region']} · {item['market_tone']}{related} · "
            f"[{item['source_name']}]({item['source_url']})"
        )
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": digest["market_summary"][:900]}},
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(lines)}},
    ]
    url = dashboard_url()
    if url:
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "查看完整看板"},
                "url": url,
                "type": "primary",
            }
        ]
        mobile_url = mobile_dashboard_url(digest)
        if mobile_url:
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "手机备用入口"},
                    "url": mobile_url,
                }
            )
        elements.append(
            {
                "tag": "action",
                "actions": actions,
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"{digest['generated_at']} · 自动汇总，仅供信息参考"}],
        }
    )
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "turquoise",
                "title": {"tag": "plain_text", "content": digest["headline"][:80]},
            },
            "elements": elements,
        },
    }


def post_feishu(payload: dict[str, Any]) -> None:
    webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        print("未配置 FEISHU_WEBHOOK_URL，跳过飞书推送")
        return
    secret = os.getenv("FEISHU_WEBHOOK_SECRET", "").strip()
    if secret:
        sign_feishu(payload, secret)
    response = request_json(webhook, payload, {}, timeout=20)
    if response.get("code", response.get("StatusCode", 0)) not in (0, None):
        raise RuntimeError(f"飞书返回失败：{response}")


def send_failure(message: str) -> None:
    try:
        post_feishu(
            {
                "msg_type": "text",
                "content": {"text": f"股市资讯看板更新失败：{message[:500]}\n上一期看板保持不变。"},
            }
        )
    except Exception as exc:
        print(f"飞书失败通知未送达：{exc}", file=sys.stderr)


def demo_digest(current: datetime) -> dict[str, Any]:
    samples = [
        ("A股", "股票", "中国市场监管动态进入观察窗口", "中国证监会", "https://www.csrc.gov.cn/", True, "中性"),
        ("港股", "ETF", "港股基金关注海外资金与科技板块变化", "香港交易所", "https://www.hkex.com.hk/", True, "混合"),
        ("美股", "股票", "美股投资者等待企业披露与政策信号", "美国证券交易委员会", "https://www.sec.gov/", True, "偏谨慎"),
        ("全球宏观", "基金", "主要央行表态仍是全球权益资产的重要变量", "Federal Reserve", "https://www.federalreserve.gov/", True, "中性"),
    ]
    items = []
    for index, (region, asset, title, source, url, official, tone) in enumerate(samples):
        items.append(
            {
                "id": hashlib.sha256(f"{url}{index}".encode()).hexdigest()[:12],
                "title": title,
                "summary": "这是用于验证看板布局和交互的固定示例，不代表当天真实市场资讯。",
                "why_it_matters": "用于检查地区、资产、来源和市场影响标签是否清晰展示。",
                "region": region,
                "asset_types": [asset],
                "published_at": (current - timedelta(hours=index + 1)).isoformat(timespec="minutes"),
                "importance": 5 - index,
                "market_tone": tone,
                "source_name": source,
                "source_url": url,
                "official": official,
            }
        )
    return {
        "version": 1,
        "date": current.date().isoformat(),
        "generated_at": current.isoformat(timespec="minutes"),
        "timezone": "Asia/Shanghai",
        "model": "demo",
        "headline": "全球市场晨间简报 · 示例",
        "market_summary": "当前展示固定示例数据，用于预览页面而非投资决策。运行自动化后，将替换为每日公开来源资讯。",
        "items": items,
        "disclaimer": "自动汇总，仅供信息参考，不构成任何投资建议。",
    }


def self_test() -> None:
    current = now_beijing()
    raw = {
        "headline": "测试日报",
        "market_summary": "测试结构与脱敏。",
        "items": [
            {
                "title": "有效资讯",
                "summary": "摘要",
                "why_it_matters": "影响",
                "region": "A股",
                "asset_types": ["股票"],
                "published_at": current.isoformat(),
                "importance": 5,
                "market_tone": "中性",
                "source_name": "官方",
                "source_url": "https://example.com/news/1",
                "official": True,
                "watchlist_match": True,
                "watchlist_symbols": ["SECRET-STOCK"],
            },
            {
                "title": "有效资讯",
                "summary": "重复",
                "why_it_matters": "重复",
                "region": "A股",
                "asset_types": ["股票"],
                "published_at": current.isoformat(),
                "importance": 4,
                "market_tone": "中性",
                "source_name": "重复",
                "source_url": "http://unsafe.example/news",
                "official": False,
                "watchlist_match": False,
                "watchlist_symbols": [],
            },
            {
                "title": "过期资讯",
                "summary": "过期",
                "why_it_matters": "过期",
                "region": "美股",
                "asset_types": ["ETF"],
                "published_at": (current - timedelta(days=8)).isoformat(),
                "importance": 3,
                "market_tone": "混合",
                "source_name": "过期来源",
                "source_url": "https://example.com/news/old",
                "official": False,
                "watchlist_match": False,
                "watchlist_symbols": [],
            },
        ],
    }
    validated = validate_digest(raw, current, {"example.com"})
    assert len(validated["items"]) == 1
    public = public_digest(validated, current)
    encoded = json.dumps(public, ensure_ascii=False)
    assert "SECRET-STOCK" not in encoded and "watchlist" not in encoded
    assert len(json.dumps(feishu_card(public, validated["items"]), ensure_ascii=False)) < 30000
    parsed = parse_watchlist('["510300", {"symbol":"AAPL", "name":"Apple"}]')
    assert parsed == ["510300", "AAPL Apple"]
    payload: dict[str, Any] = {}
    sign_feishu(payload, "test-secret")
    assert payload.get("timestamp") and payload.get("sign")
    assert not has_term("retail trader", "trade")
    assert not is_relevant("Startup closes a seed funding round")
    print("self-test: ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="每日国内外股市资讯看板生成器")
    parser.add_argument("--self-test", action="store_true", help="运行内置验证，不联网、不写文件")
    parser.add_argument("--demo", action="store_true", help="写入固定示例数据，用于本地预览")
    parser.add_argument("--notify", action="store_true", help="将现有 latest.json 推送到飞书")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0
    if args.notify:
        try:
            digest = json.loads((DATA_DIR / "latest.json").read_text(encoding="utf-8"))
            private_path = ROOT / ".private-items.json"
            private_items = json.loads(private_path.read_text(encoding="utf-8")) if private_path.exists() else []
            post_feishu(feishu_card(digest, private_items))
        except Exception as exc:
            print(f"飞书推送失败（看板已保留）：{exc}", file=sys.stderr)
        return 0

    current = now_beijing()
    try:
        if args.demo:
            digest = demo_digest(current)
        else:
            watchlist = parse_watchlist(os.getenv("WATCHLIST_JSON", ""))
            raw = collect_digest(watchlist, current)
            private_digest = validate_digest(raw, current)
            digest = public_digest(private_digest, current)
            atomic_write_json(
                ROOT / ".private-items.json",
                [
                    {"id": item["id"], "watchlist_match": item["watchlist_match"]}
                    for item in private_digest["items"]
                ],
            )
        save_digest(digest, current)
        print(f"已生成 {digest['date']} 看板，共 {len(digest['items'])} 条资讯")
        return 0
    except Exception as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        send_failure(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
