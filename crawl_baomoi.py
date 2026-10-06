import json
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.sync_api import sync_playwright


START_URL = "https://baomoi.com/tin-moi.epi"
OUTPUT_FILE = "baomoi_24h.json"

MAX_RUNTIME_SECONDS = 900
MAX_IDLE_ROUNDS = 10

NEXT_PAGE_PATTERN = re.compile(r"/tin-moi/trang\d+\.epi")


def response_kind(url):
    parsed = urlparse(url)

    if (
        parsed.hostname == "w-api.baomoi.com"
        and parsed.path == "/api/v1/content/get/list-by-type"
    ):
        params = parse_qs(parsed.query)
        if params.get("listType") == ["3"]:
            return "article_api"

    # Ghi nhận JSON từ Báo Mới khi chuyển trang.
    # Cấu trúc response loại này chưa được xác minh.
    if (
        parsed.hostname == "baomoi.com"
        and parsed.path.endswith(".json")
    ):
        return "page_json"

    return None


def response_label(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    page_number = params.get("page", ["?"])[0]
    return f"{parsed.path}, page={page_number}"


def extract_article_objects(value):
    """Tìm object có cấu trúc bài giống JSON mẫu anh đã gửi."""
    if isinstance(value, dict):
        article_id = value.get("id", value.get("contentId"))

        if (
            isinstance(article_id, int)
            and isinstance(value.get("title"), str)
            and isinstance(value.get("date"), (int, float))
            and isinstance(value.get("url"), str)
        ):
            yield value
            return

        for child in value.values():
            yield from extract_article_objects(child)

    elif isinstance(value, list):
        for child in value:
            yield from extract_article_objects(child)


def scroll_to_bottom(page):
    page.evaluate("""
        () => {
            const root = document.scrollingElement;
            if (root) {
                root.scrollTo({
                    top: root.scrollHeight,
                    behavior: "instant"
                });
            }
        }
    """)


def find_next_page(page):
    links = page.locator('a[href*="/tin-moi/trang"]')

    for index in range(links.count()):
        link = links.nth(index)

        if not link.is_visible():
            continue

        text = " ".join(link.inner_text().split())
        if "xem thêm" not in text.casefold():
            continue

        href = link.get_attribute("href")
        if not href:
            continue

        absolute_url = urljoin(page.url, href)
        parsed = urlparse(absolute_url)

        if (
            parsed.hostname == "baomoi.com"
            and NEXT_PAGE_PATTERN.fullmatch(parsed.path)
        ):
            return absolute_url

    return None


def main():
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=24)
    deadline = time.monotonic() + MAX_RUNTIME_SECONDS

    pending = deque()
    articles = {}
    seen_ids = set()
    visited_pages = set()
    errors = []

    batch_count = 0
    idle_rounds = 0
    reached_old_article = False
    stop_reason = "runtime_limit"

    def process_payload(payload, source):
        nonlocal reached_old_article

        found_count = 0
        new_count = 0

        for item in extract_article_objects(payload):
            found_count += 1
            article_id = item.get("id", item.get("contentId"))

            if article_id in seen_ids:
                continue

            try:
                article_time = datetime.fromtimestamp(
                    item["date"], tz=timezone.utc
                )
                url = urljoin("https://baomoi.com/", item["url"])

                # Chỉ nhận đường dẫn bài, không nhận object khác.
                if (
                    urlparse(url).hostname != "baomoi.com"
                    or not re.search(r"-c\d+\.epi$", urlparse(url).path)
                ):
                    continue

                if article_time < window_start:
                    reached_old_article = True

                if window_start <= article_time < window_end:
                    publisher = item.get("publisher")
                    publisher_name = (
                        publisher.get("name")
                        if isinstance(publisher, dict)
                        else None
                    )

                    articles[article_id] = {
                        "id": article_id,
                        "title": item["title"],
                        "url": url,
                        "date_raw": item["date"],
                        "date_utc": article_time.isoformat(),
                        "publisher": publisher_name,
                        "description": item.get("description", ""),
                        "collected_from": source,
                    }

                seen_ids.add(article_id)
                new_count += 1

            except Exception as exc:
                message = f"Lỗi bài {article_id}: {exc}"
                errors.append(message)
                print(message)

        print(
            f"DATA [{source}]: tìm {found_count} object bài, "
            f"{new_count} ID mới; "
            f"đã lưu {len(articles)} bài trong 24 giờ"
        )
        return new_count

    def process_embedded_json(page):
        # Không giả định website chắc chắn có __NEXT_DATA__.
        # Chỉ đọc những script chứa JSON hợp lệ.
        texts = page.locator(
            'script[type="application/json"], script#__NEXT_DATA__'
        ).evaluate_all("(nodes) => nodes.map(node => node.textContent)")

        new_count = 0

        for text in texts:
            try:
                payload = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                continue

            new_count += process_payload(payload, "embedded_json")

        return new_count

    with sync_playwright() as p:
        browser = p.firefox.launch(headless=False)

        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 900}
            )

            def log_request(request):
                if response_kind(request.url):
                    print("SEND:", response_label(request.url))

            def collect_response(response):
                kind = response_kind(response.url)
                if kind:
                    print(
                        f"RECEIVE [{kind}]: HTTP {response.status}; "
                        f"{response_label(response.url)}"
                    )
                    pending.append((kind, response))

            def log_failed(request):
                if response_kind(request.url):
                    message = (
                        f"FAILED: {response_label(request.url)}; "
                        f"{request.failure}"
                    )
                    errors.append(message)
                    print(message)

            page.on("request", log_request)
            page.on("response", collect_response)
            page.on("requestfailed", log_failed)

            page.goto(
                START_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            visited_pages.add(page.url)
            process_embedded_json(page)

            while time.monotonic() < deadline:
                page.wait_for_timeout(2000)
                progress = False

                while pending:
                    kind, response = pending.popleft()

                    try:
                        if response.status != 200:
                            raise RuntimeError(
                                f"HTTP {response.status}: "
                                f"{response_label(response.url)}"
                            )

                        payload = response.json()

                        if kind == "article_api":
                            if payload.get("err") != 0:
                                raise RuntimeError(
                                    f"API err={payload.get('err')}; "
                                    f"msg={payload.get('msg')}"
                                )

                            print(
                                "API hasMore:",
                                payload.get("data", {}).get("hasMore"),
                            )

                        batch_count += 1
                        new_count = process_payload(payload, kind)
                        progress = progress or new_count > 0

                        if kind == "page_json" and new_count == 0:
                            print(
                                "NOTE: JSON trang không có ID mới. "
                                "Có thể trùng dữ liệu hoặc khác cấu trúc."
                            )

                    except Exception as exc:
                        errors.append(str(exc))
                        print("Lỗi đọc response:", exc)

                idle_rounds = 0 if progress else idle_rounds + 1

                if idle_rounds >= MAX_IDLE_ROUNDS:
                    print(
                        "\nKhông có ID bài mới sau nhiều vòng. "
                        "Anh có 30 giây để thử thao tác tay "
                        "trong Firefox này.\n"
                    )
                    page.wait_for_timeout(30_000)

                    if pending:
                        # Xử lý dữ liệu ở vòng kế tiếp;
                        # không tự coi response trùng là tiến độ mới.
                        idle_rounds = 0
                        continue

                    stop_reason = "no_progress"
                    break

                scroll_to_bottom(page)
                page.wait_for_timeout(1000)

                # Nếu cuộn vừa tạo response, đọc trước khi chuyển trang.
                if pending:
                    continue

                next_url = find_next_page(page)

                if next_url:
                    if next_url in visited_pages:
                        print("SKIP: URL đã mở:", next_url)
                    else:
                        print("NAVIGATE:", next_url)

                        # Theo URL thật của nút, không tự đoán số trang.
                        page.goto(
                            next_url,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        visited_pages.add(page.url)

                        new_count = process_embedded_json(page)
                        if new_count:
                            idle_rounds = 0

                        continue

                # Tiếp tục kích hoạt infinite scroll gần cuối trang.
                page.mouse.move(640, 600)
                page.mouse.wheel(0, -500)
                page.wait_for_timeout(400)
                page.mouse.wheel(0, 1000)
                print("ACTION: cuộn gần cuối trang")

        except KeyboardInterrupt:
            stop_reason = "user_interrupted"
            print("\nĐã dừng; lưu dữ liệu đã thu được.")

        except Exception as exc:
            stop_reason = "error"
            errors.append(str(exc))
            print("Lỗi khi chạy:", exc)

        finally:
            browser.close()

    result = {
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": window_end.isoformat(),
        "time_basis": "Trường date của dữ liệu Báo Mới",
        "stop_reason": stop_reason,
        "completeness_verified": False,
        "reached_article_older_than_window": reached_old_article,
        "batch_count": batch_count,
        "visited_pages": sorted(visited_pages),
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

    print(f"\nĐã lưu {len(articles)} bài vào {OUTPUT_FILE}")
    print("Lý do dừng:", stop_reason)


if __name__ == "__main__":
    main()
