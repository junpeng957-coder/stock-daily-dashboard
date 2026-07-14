#!/usr/bin/env python3
"""Generate, validate, archive and optionally push the daily market digest."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "docs" / "data"
BEIJING = ZoneInfo("Asia/Shanghai")
OPENAI_URL = "https://api.openai.com/v1/responses"
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
REGIONS = {"A股", "港股", "美股", "全球宏观"}
ASSETS = {"股票", "基金", "ETF"}
TONES = {"偏积极", "偏谨慎", "中性", "混合"}
BLOCKED_HOSTS = {"reddit.com", "www.reddit.com", "quora.com", "www.quora.com", "wikipedia.org", "www.wikipedia.org"}


ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "region": {"type": "string", "enum": sorted(REGIONS)},
        "asset_types": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(ASSETS)},
        },
        "published_at": {"type": "string"},
        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
        "market_tone": {"type": "string", "enum": sorted(TONES)},
        "source_name": {"type": "string"},
        "source_url": {"type": "string"},
        "official": {"type": "boolean"},
        "watchlist_match": {"type": "boolean"},
        "watchlist_symbols": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title",
        "summary",
        "why_it_matters",
        "region",
        "asset_types",
        "published_at",
        "importance",
        "market_tone",
        "source_name",
        "source_url",
        "official",
        "watchlist_match",
        "watchlist_symbols",
    ],
}

DIGEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "headline": {"type": "string"},
        "market_summary": {"type": "string"},
        "items": {"type": "array", "items": ITEM_SCHEMA, "minItems": 1, "maxItems": 15},
    },
    "required": ["headline", "market_summary", "items"],
}


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


def build_prompt(current: datetime, watchlist: list[str]) -> str:
    cutoff = current - timedelta(hours=36)
    watchlist_text = json.dumps(watchlist, ensure_ascii=False) if watchlist else "[]"
    return f"""你是谨慎的中文金融资讯编辑。当前北京时间是 {current.isoformat(timespec='minutes')}。
请实际联网搜索并整理 {cutoff.isoformat(timespec='minutes')} 至今发布的股票、基金和 ETF 资讯。

覆盖范围：A股、港股、美股，以及会直接影响这些市场的全球央行、监管和宏观事件。
来源优先级：监管机构/交易所/央行/上市公司原始发布 > Reuters、Bloomberg、FT、CNBC 等主流财经媒体 > 机构公开内容。公开社交媒体只能补充已被可靠来源确认的信息；排除匿名爆料、荐股、加密货币、商品期货、房地产和泛经济新闻。

要求：
1. 返回 8 至 15 条；若可靠资讯不足可以更少，绝不凑数。
2. 每条只使用真实且直接指向原文的 HTTPS 链接，不使用搜索结果页或聚合跳转页。
3. summary 最多两句；why_it_matters 用一句话说明可能影响，不给出买卖建议。
4. importance 用 1-5 表示重要性；official 仅在来源确为官方机构或公司原始发布时为 true。
5. 去除同一事件的重复报道，优先保留原始来源或信息量最大的一条。
6. 自选清单仅用于排序和匹配，禁止在 headline 或 market_summary 中披露清单：{watchlist_text}
7. watchlist_symbols 只填确实匹配的自选项，否则为空数组。
8. headline 是简短日报标题；market_summary 是 2-4 句客观总览。
"""


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


def extract_output_text(response: dict[str, Any]) -> str:
    for output in response.get("output", []):
        if output.get("type") != "message":
            continue
        for content in output.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"]
    raise ValueError("OpenAI 响应中没有可解析的 output_text")


def collect_source_hosts(response: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str):
                host = urllib.parse.urlparse(url).hostname
                if host:
                    hosts.add(host.lower().removeprefix("www."))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(response.get("output", []))
    return hosts


def fetch_digest(api_key: str, watchlist: list[str], current: datetime) -> tuple[dict[str, Any], set[str]]:
    payload = {
        "model": MODEL,
        "store": False,
        "reasoning": {"effort": "low"},
        "tools": [
            {
                "type": "web_search",
                "search_context_size": "medium",
                "filters": {"blocked_domains": sorted(BLOCKED_HOSTS)},
            }
        ],
        "tool_choice": "auto",
        "include": ["web_search_call.action.sources"],
        "input": build_prompt(current, watchlist),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "daily_market_digest",
                "strict": True,
                "schema": DIGEST_SCHEMA,
            }
        },
        "max_output_tokens": 12000,
    }
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = request_json(OPENAI_URL, payload, {"Authorization": f"Bearer {api_key}"})
            return json.loads(extract_output_text(response)), collect_source_hosts(response)
        except Exception as exc:  # retry once for transient provider/network failures
            last_error = exc
            if attempt == 0:
                time.sleep(3)
    raise RuntimeError(f"OpenAI 生成失败（已重试一次）：{last_error}")


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

    cutoff = current - timedelta(hours=36)
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
    return {"headline": headline, "market_summary": summary, "items": items[:15]}


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
        "model": MODEL,
        "headline": validated["headline"],
        "market_summary": validated["market_summary"],
        "items": items,
        "disclaimer": "AI 汇总，仅供信息参考，不构成任何投资建议。",
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


def dashboard_url() -> str:
    explicit = os.getenv("DASHBOARD_URL", "").strip()
    if explicit:
        return explicit.rstrip("/") + "/"
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in repository:
        owner, name = repository.split("/", 1)
        return f"https://{owner}.github.io/{name}/"
    return ""


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
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看完整看板"},
                        "url": url,
                        "type": "primary",
                    }
                ],
            }
        )
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"{digest['generated_at']} · AI 汇总，仅供信息参考"}],
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
        "market_summary": "当前展示固定示例数据，用于预览页面而非投资决策。配置 API 密钥并运行自动化后，将替换为每日联网资讯。",
        "items": items,
        "disclaimer": "AI 汇总，仅供信息参考，不构成任何投资建议。",
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
                "published_at": (current - timedelta(hours=40)).isoformat(),
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
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise ValueError("缺少 OPENAI_API_KEY")
            watchlist = parse_watchlist(os.getenv("WATCHLIST_JSON", ""))
            raw, source_hosts = fetch_digest(api_key, watchlist, current)
            private_digest = validate_digest(raw, current, source_hosts)
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
