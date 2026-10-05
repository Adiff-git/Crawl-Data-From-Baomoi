import argparse
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright


MAX_RUNTIME_SECONDS = 600
MAX_IDLE_ROUNDS = 10
OUTPUT_FILE = "baomoi_24h.json"


def is_article_list(response):
    url = urlparse(response.url)
    return (
        url.hostname == "w-api.baomoi.com"
        and url.path == "/api/v1/content/get/list-by-type"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Thu thập bài viết mới từ trang Tin mới của Báo Mới."
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Hiện cửa sổ Chromium khi chạy (mặc định chạy headless).",
    )
    args = parser.parse_args()

    # Cửa sổ cố định cho toàn bộ lần chạy.
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=24)

    pending = deque()
    articles = {}
    seen_ids = set()

    batch_count = 0
    idle_rounds = 0
    reached_old_article = False
    stop_reason = "runtime_limit"
    errors = []

    deadline = time.monotonic() + MAX_RUNTIME_SECONDS

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)

        try:
            page = browser.new_page()

            def collect_response(response):
                if is_article_list(response):
                    pending.append(response)

            page.on("response", collect_response)

            page.goto(
                "https://baomoi.com/tin-moi.epi",
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            while time.monotonic() < deadline:
                # Cho trình duyệt thời gian nhận response.
                page.wait_for_timeout(2000)

                received_batch = False
                no_more = False

                while pending:
                    response = pending.popleft()

                    try:
                        if response.status != 200:
                            raise RuntimeError(f"HTTP {response.status}")

                        payload = response.json()
                        if payload.get("err") != 0:
                            raise RuntimeError(
                                f"API error: {payload.get('msg')}"
                            )

                        data = payload["data"]
                        items = data["items"]

                        received_batch = True
                        batch_count += 1

                        for item in items:
                            article_id = item["id"]
                            if article_id in seen_ids:
                                continue

                            article_time = datetime.fromtimestamp(
                                item["date"], tz=timezone.utc
                            )
                            seen_ids.add(article_id)

                            if article_time < window_start:
                                reached_old_article = True

                            if window_start <= article_time < window_end:
                                articles[article_id] = {
                                    "id": article_id,
                                    "title": item["title"],
                                    "url": urljoin(
                                        "https://baomoi.com/",
                                        item["url"],
                                    ),
                                    "date_raw": item["date"],
                                    "date_utc": article_time.isoformat(),
                                    "publisher": item.get(
                                        "publisher", {}
                                    ).get("name"),
                                    "description": item.get(
                                        "description", ""
                                    ),
                                }

                        print(
                            f"Đợt {batch_count}: nhận {len(items)} mục; "
                            f"đã giữ {len(articles)} bài trong 24 giờ"
                        )

                        if data.get("hasMore") is False:
                            no_more = True

                    except Exception as exc:
                        errors.append(str(exc))
                        print("Lỗi đọc response:", exc)

                if no_more:
                    stop_reason = "server_has_no_more"
                    break

                idle_rounds = 0 if received_batch else idle_rounds + 1

                if idle_rounds >= MAX_IDLE_ROUNDS:
                    stop_reason = "no_new_response"
                    break

                page.mouse.wheel(0, 2500)

        except Exception as exc:
            stop_reason = "error"
            errors.append(str(exc))
            print("Lỗi khi chạy:", exc)

        finally:
            browser.close()

    result = {
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": window_end.isoformat(),
        "time_basis": "Trường date của API Báo Mới",
        "stop_reason": stop_reason,
        "reached_article_older_than_window": reached_old_article,
        "batch_count": batch_count,
        "article_count": len(articles),
        "errors": errors,
        "articles": sorted(
            articles.values(),
            key=lambda article: article["date_raw"],
            reverse=True,
        ),
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Đã lưu {len(articles)} bài vào {OUTPUT_FILE}")
    print("Lý do dừng:", stop_reason)


if __name__ == "__main__":
    main()
