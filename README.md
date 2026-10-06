# Crawl Data From Báo Mới

Script Python thu thập bài viết từ mục [Tin mới của Báo Mới](https://baomoi.com/tin-moi.epi), chỉ lưu các bài có thời gian đăng trong 24 giờ trước lúc bắt đầu chạy. Script dùng Playwright để mở Firefox, đọc dữ liệu JSON mà trang tải, cuộn và đi theo liên kết "Xem thêm" để lấy các trang tiếp theo. Bài viết được loại trùng theo ID và ghi vào `baomoi_24h.json`.

## Yêu cầu

- Python 3.9 trở lên.
- Kết nối mạng và môi trường có thể mở cửa sổ Firefox.
- Firefox do Playwright cài đặt.

## Cài đặt

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install firefox
```

Trên Windows, kích hoạt môi trường bằng `.venv\Scripts\activate` trong Command Prompt hoặc `.venv\Scripts\Activate.ps1` trong PowerShell.

## Chạy

```bash
python crawl_baomoi.py
```

Script mở Firefox có giao diện. Nếu không có bài mới sau nhiều lượt chờ, chương trình dành thêm 30 giây để thao tác trực tiếp trong cửa sổ trình duyệt. Script dừng sau tối đa 15 phút, khi không có thêm tiến độ, khi gặp lỗi hoặc khi nhấn Ctrl+C; dữ liệu đã thu thập vẫn được lưu.

File `baomoi_24h.json` được tạo trong thư mục hiện tại. File chứa mốc thời gian UTC, số lượt phản hồi, các trang đã mở, số bài, lý do dừng, lỗi nếu có và danh sách bài viết. Mỗi bài có ID, tiêu đề, URL, thời gian đăng UTC, nguồn, mô tả và loại phản hồi nơi bài được tìm thấy.

## Phạm vi kết quả

Script chỉ giữ bài nằm trong cửa sổ 24 giờ và thu thập từ các trang Tin mới mà nó truy cập được. Trường `completeness_verified` hiện luôn là `false`: kết quả chưa có bước đối chiếu độc lập để xác nhận đã lấy đủ toàn bộ bài trong 24 giờ. `reached_article_older_than_window` cho biết quá trình thu thập có gặp bài cũ hơn mốc bắt đầu hay chưa; riêng dấu hiệu này cũng không chứng minh tính đầy đủ.

Trang web và API có thể thay đổi, làm script cần cập nhật. Hãy sử dụng dữ liệu theo điều khoản của Báo Mới và nguồn xuất bản.

## Thu thập VnExpress

`crawl_vnexpress.py` thu thập bài từ [Tin tức 24h của VnExpress](https://vnexpress.net/tin-tuc-24h) trong cửa sổ 24 giờ và đọc số bình luận, lượt thích từ phản hồi công khai của trang. Script cũng lưu thứ hạng theo số bình luận và điểm tương tác (tổng lượt thích bình luận cộng số trả lời). Kết quả là ảnh chụp tại thời điểm chạy; phạm vi toàn bộ trang VnExpress chưa được xác nhận.

Sau khi cài Playwright và Firefox theo hướng dẫn trên, chạy:

```bash
python crawl_vnexpress.py
```

Script mở Firefox có giao diện và ghi kết quả vào `vnexpress_24h.json`. Có thể dùng `--headless` để chạy không giao diện, `--output` để đổi đường dẫn file, hoặc `--max-articles` để giới hạn số bài khi thử. File kết quả có `ranking_is_partial` và thông tin lỗi để nhận biết thứ hạng có thể chưa đầy đủ. Cả hai file JSON kết quả đều được bỏ qua trong Git.
