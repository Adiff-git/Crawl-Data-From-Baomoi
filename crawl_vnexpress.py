"""Collect VnExpress latest-list articles and public comment statistics.

Install: python -m pip install playwright
         python -m playwright install firefox
Run:     python crawl_vnexpress.py
This is a best-effort snapshot of the latest listing, not all-site coverage.
"""
import argparse
import json
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import sync_playwright

BASE = "https://vnexpress.net"
START = BASE + "/tin-tuc-24h"
COMMENT_HOST = "usi-saas.vnexpress.net"
VN = timezone(timedelta(hours=7))
MAX_COMMENT_PAGES = 200


def integer(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a count")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isdigit():
        number = int(value)
    else:
        raise ValueError(f"Invalid integer: {value!r}")
    if number < 0:
        raise ValueError("Negative count")
    return number


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        # Explicit convention for timezone-less VnExpress date metadata.
        if result.tzinfo is None:
            result = result.replace(tzinfo=VN)
        return result.astimezone(timezone.utc)
    except ValueError:
        return None


def json_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_nodes(child)


def publication_time(page):
    for selector in (
        'meta[property="article:published_time"]',
        'meta[itemprop="datePublished"]',
        'meta[name="pubdate"]',
    ):
        node = page.locator(selector).first
        if node.count():
            result = parse_time(node.get_attribute("content"))
            if result:
                return result, selector
    scripts = page.locator('script[type="application/ld+json"]').all_text_contents()
    for script in scripts:
        try:
            payload = json.loads(script)
        except (ValueError, TypeError):
            continue
        for node in json_nodes(payload):
            kinds = node.get("@type", [])
            if isinstance(kinds, str):
                kinds = [kinds]
            if any(kind in ("NewsArticle", "Article", "BlogPosting") for kind in kinds):
                result = parse_time(node.get("datePublished"))
                if result:
                    return result, "JSON-LD datePublished"
    # Last fallback: explicit visible calendar date, never relative time.
    nodes = page.locator(".header-content .date, .date")
    for index in range(min(nodes.count(), 10)):
        text = nodes.nth(index).inner_text()
        match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4}),\s*(\d{1,2}):(\d{2})", text)
        if match and "GMT+7" in text:
            day, month, year, hour, minute = map(int, match.groups())
            return datetime(year, month, day, hour, minute, tzinfo=VN).astimezone(timezone.utc), "visible GMT+7 date"
    return None, None


def validate_payload(payload):
    if not isinstance(payload, dict) or payload.get("error") != 0:
        raise ValueError("Comment API returned an error or an unexpected payload")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Missing data.items")
    return data


def modified_url(template, updates):
    parsed = urlparse(template)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params.update({key: [str(value)] for key, value in updates.items()})
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True)))


def fetch_json(context, page, url):
    # Reuse current browser cookies. Do not hard-code the user's cookie_aid/sign.
    page.wait_for_timeout(700)
    response = context.request.get(
        url,
        headers={"Accept": "application/json, text/javascript, */*; q=0.01",
                 "User-Agent": page.evaluate("() => navigator.userAgent"),
                 "Referer": BASE + "/", "Origin": BASE},
        timeout=30000,
    )
    try:
        if response.status != 200:
            raise RuntimeError(f"Comment HTTP {response.status}")
        return response.json()
    finally:
        response.dispose()


def normalized_comment(item, article_id):
    if integer(item["article_id"]) != article_id:
        raise ValueError("Comment belongs to a different article")
    return {
        "comment_id": integer(item["comment_id"]),
        "parent_id": integer(item["parent_id"]),
        "content": item.get("content", ""),
        "author": item.get("full_name", ""),
        "creation_time": item.get("creation_time"),
        "likes": integer(item["userlike"]),
    }


def collect_comments(context, page, template, initial, article_id):
    data = validate_payload(initial)
    expected_roots = integer(data["total"])
    expected_all = integer(data["totalitem"])
    roots, replies, errors = {}, {}, []
    initial_params = parse_qs(urlparse(template).query)
    initial_offset = integer(initial_params.get("offset", ["0"])[0])
    if initial_offset != 0:
        raise ValueError("Captured first comment request has nonzero offset")
    offset = 0
    root_complete = False

    for _ in range(MAX_COMMENT_PAGES):
        items = data["items"]
        previous_count = len(roots)
        for item in items + (data.get("items_pin") or []):
            comment = normalized_comment(item, article_id)
            if comment["parent_id"] != comment["comment_id"]:
                raise ValueError("Unexpected reply in root list")
            reply_info = item.get("replys") or {}
            comment["expected_replies"] = integer(reply_info.get("total", 0))
            roots[comment["comment_id"]] = comment
            for embedded in reply_info.get("items", []) or []:
                reply = normalized_comment(embedded, article_id)
                if reply["parent_id"] != comment["comment_id"]:
                    raise ValueError("Embedded reply has unexpected parent")
                replies[reply["comment_id"]] = reply
        if len(roots) >= expected_roots:
            root_complete = len(roots) == expected_roots
            break
        if not items or len(roots) == previous_count:
            errors.append("Root pagination ended or repeated before expected total")
            break
        offset += len(items)
        # Whether sign permits an offset change is not assumed: API errors
        # make this article incomplete instead of silently producing a score.
        data = validate_payload(fetch_json(context, page, modified_url(template, {"offset": offset})))
    else:
        errors.append("Root pagination limit reached")

    for root in roots.values():
        parent_id = root["comment_id"]
        expected = root["expected_replies"]
        loaded = {cid for cid, reply in replies.items() if reply["parent_id"] == parent_id}
        if len(loaded) >= expected:
            continue
        # Endpoint and keys are grounded in the user's getreplay cURL.
        params = {
            "siteid": initial_params["siteid"][0],
            "objectid": article_id, "objecttype": 1,
            "id": parent_id, "limit": 12, "offset": 0,
            "sort_by": "like",
        }
        if "cookie_aid" in initial_params:
            params["cookie_aid"] = initial_params["cookie_aid"][0]
        offset = 0
        try:
            for _ in range(MAX_COMMENT_PAGES):
                params["offset"] = offset
                url = f"https://{COMMENT_HOST}/index/getreplay?" + urlencode(params)
                reply_data = validate_payload(fetch_json(context, page, url))
                items = reply_data["items"]
                before = len(loaded)
                for item in items:
                    reply = normalized_comment(item, article_id)
                    if reply["parent_id"] != parent_id:
                        raise ValueError("Reply has unexpected parent")
                    replies[reply["comment_id"]] = reply
                    loaded.add(reply["comment_id"])
                if len(loaded) >= expected:
                    break
                if not items or len(loaded) == before:
                    raise ValueError("Reply pagination ended or repeated early")
                offset += len(items)
            else:
                raise ValueError("Reply pagination limit reached")
        except Exception as exc:
            errors.append(f"Replies for {parent_id}: {type(exc).__name__}")

    complete = (
        root_complete and not errors
        and len(roots) + len(replies) == expected_all
        and all(sum(r["parent_id"] == c["comment_id"] for r in replies.values()) == c["expected_replies"] for c in roots.values())
    )
    likes = sum(c["likes"] for c in list(roots.values()) + list(replies.values()))
    return {
        "comment_count": expected_all,
        "root_comment_count": expected_roots,
        "loaded_root_count": len(roots), "loaded_reply_count": len(replies),
        "total_comment_likes": likes,
        "interaction_score": likes + len(replies) if complete else None,
        "partial_interaction_score": likes + len(replies),
        "comments_complete": complete, "errors": errors,
        "comments": list(roots.values()) + list(replies.values()),
        "metrics_fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def capture_initial_comments(page, queue, article_id):
    for _ in range(20):
        page.wait_for_timeout(1000)
        while queue:
            response = queue.popleft()
            params = parse_qs(urlparse(response.url).query)
            if params.get("objectid") != [str(article_id)]:
                continue
            if params.get("offset", ["0"]) != ["0"]:
                continue
            if response.status != 200:
                raise RuntimeError(f"Initial comments HTTP {response.status}")
            payload = response.json()
            validate_payload(payload)
            return response.url, payload
        # Use the observed public comment container if present; otherwise
        # scroll progressively to activate lazy comment loading.
        target = page.locator("#box_comment, #box_comment_vne").first
        if target.count():
            target.scroll_into_view_if_needed(timeout=5000)
        else:
            page.mouse.wheel(0, 1500)
    raise RuntimeError("No initial comment response; count remains unknown")


def discover_articles(page):
    pending, visited, articles = deque([START]), set(), {}
    while pending and len(visited) < 20:
        url = pending.popleft()
        if url in visited:
            continue
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if not response or response.status != 200:
            raise RuntimeError("Listing did not return HTTP 200")
        visited.add(url)
        page.wait_for_timeout(500)
        links = page.locator("article.item-news h3.title-news a").evaluate_all(
            "nodes => nodes.map(a => ({url: a.href, title: a.textContent.trim()}))"
        )
        if not links:
            raise RuntimeError("No listing links found; inspect selector")
        for link in links:
            parsed = urlparse(link["url"])
            match = re.search(r"-(\d+)\.html$", parsed.path)
            if match and parsed.hostname == "vnexpress.net":
                article_id = int(match.group(1))
                articles[article_id] = {"article_id": article_id, **link}
        hrefs = page.locator('a[href*="tin-tuc-24h"]').evaluate_all("nodes => nodes.map(a => a.href)")
        for href in hrefs:
            parsed = urlparse(href)
            if parsed.hostname == "vnexpress.net" and re.fullmatch(r"/tin-tuc-24h(?:-p\d+)?", parsed.path):
                canonical = BASE + parsed.path
                if canonical not in visited and canonical not in pending:
                    pending.append(canonical)
        print(f"LIST: {url}; unique articles={len(articles)}")
    return list(articles.values()), sorted(visited), not pending


def save_result(path, result):
    rows = result["articles"]
    known = [r for r in rows if r.get("comment_count") is not None]
    complete = [r for r in rows if r.get("comments_complete") and r.get("interaction_score") is not None]
    result["ranking_by_comment_count"] = [r["article_id"] for r in sorted(known, key=lambda r: (-r["comment_count"], r["article_id"]))]
    result["ranking_by_interaction"] = [r["article_id"] for r in sorted(complete, key=lambda r: (-r["interaction_score"], r["article_id"]))]
    result["ranking_is_partial"] = bool(result["failures"] or result["time_unknown"] or result.get("processed_candidates", 0) < result.get("candidate_count", 0) or any(not r.get("comments_complete") for r in rows))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="vnexpress_24h.json")
    parser.add_argument("--max-articles", type=int, default=0, help="0 = all discovered candidates; positive = trial limit")
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.max_articles < 0:
        parser.error("--max-articles must be >= 0")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    output = Path(args.output)
    result = {
        "window_start_utc": start.isoformat(), "window_end_utc": end.isoformat(),
        "coverage": "Articles discovered in VnExpress tin-tuc-24h listing; all-site completeness not verified",
        "interaction_formula": "likes on all unique roots/replies + number of unique replies",
        "metrics_note": "Live snapshots, not a simultaneous snapshot; sort_by=like may change during pagination",
        "articles": [], "failures": [], "time_unknown": [],
        "processed_candidates": 0, "stop_reason": "running",
    }
    try:
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=args.headless)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            queue = deque()
            def on_response(response):
                parsed = urlparse(response.url)
                if parsed.hostname == COMMENT_HOST and parsed.path == "/index/get":
                    queue.append(response)
            page.on("response", on_response)
            try:
                candidates, listing_urls, listing_complete = discover_articles(page)
                result.update(candidate_count=len(candidates), listing_urls=listing_urls, listing_pagination_exhausted=listing_complete)
                selected = candidates[:args.max_articles] if args.max_articles else candidates
                for index, article in enumerate(selected, 1):
                    print(f"ARTICLE {index}/{len(selected)}: {article['title']}")
                    queue.clear()
                    try:
                        response = page.goto(article["url"], wait_until="domcontentloaded", timeout=60000)
                        if not response or response.status != 200:
                            raise RuntimeError("Article did not return HTTP 200")
                        published, time_source = publication_time(page)
                        if not published:
                            result["time_unknown"].append(article)
                        elif start <= published < end:
                            row = {**article, "published_at": published.isoformat(), "time_source": time_source,
                                   "comment_count": None, "comments_complete": False, "interaction_score": None}
                            result["articles"].append(row)
                            try:
                                template, initial = capture_initial_comments(page, queue, article["article_id"])
                                # Save known total even if later pagination fails.
                                row["comment_count"] = integer(initial["data"]["totalitem"])
                                row.update(collect_comments(context, page, template, initial, article["article_id"]))
                                print(f"COMMENTS: total={row['comment_count']}; complete={row['comments_complete']}; score={row['interaction_score']}")
                            except Exception as exc:
                                row["error"] = type(exc).__name__ + ": comment collection failed"
                                result["failures"].append({"article_id": article["article_id"], "stage": "comments", "error": type(exc).__name__})
                                print("COMMENTS FAILED:", type(exc).__name__)
                    except Exception as exc:
                        result["failures"].append({"article_id": article["article_id"], "stage": "article", "error": type(exc).__name__})
                        print("ARTICLE FAILED:", type(exc).__name__)
                    result["processed_candidates"] += 1
                    save_result(output, result)
                result["stop_reason"] = "trial_limit" if len(selected) < len(candidates) else "listing_processed"
            finally:
                browser.close()
    except KeyboardInterrupt:
        result["stop_reason"] = "user_interrupted"
    except Exception as exc:
        result["stop_reason"] = "error"
        result["failures"].append({"stage": "run", "error": type(exc).__name__})
        print("RUN FAILED:", type(exc).__name__)
    finally:
        save_result(output, result)
    print(f"Saved {len(result['articles'])} articles to {output}")
    print("Stop:", result["stop_reason"], "; partial ranking:", result["ranking_is_partial"])
    by_id = {row["article_id"]: row for row in result["articles"]}
    for key in ("ranking_by_comment_count", "ranking_by_interaction"):
        print(key)
        for article_id in result[key][:5]:
            row = by_id[article_id]
            print(row["comment_count"], row.get("interaction_score"), row["title"], row["url"])


if __name__ == "__main__":
    main()
