# Crawl Data From Báo Mới

Script Python thu thập các bài viết có thời gian đăng nằm trong 24 giờ trước lúc bắt đầu chạy, từ trang [Tin mới của Báo Mới](https://baomoi.com/tin-moi.epi). Script lắng nghe phản hồi danh sách bài viết mà trang tải trong Chromium, cuộn trang để nhận thêm dữ liệu, loại trùng theo ID và ghi kết quả ra `baomoi_24h.json`.

## Yêu cầu

- Python 3.9 trở lên.
- Kết nối mạng để truy cập Báo Mới.
- Chromium do Playwright cài đặt.

## Cài đặt

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Trên Windows, kích hoạt môi trường bằng `.venv\Scripts\activate` trong Command Prompt hoặc `.venv\Scripts\Activate.ps1` trong PowerShell.

## Chạy

```bash
python crawl_baomoi.py
```

Chương trình chạy Chromium không giao diện theo mặc định. Để xem cửa sổ trình duyệt khi gỡ lỗi:

```bash
python crawl_baomoi.py --headed
```

File `baomoi_24h.json` được tạo trong thư mục hiện tại. File gồm khoảng thời gian thu thập (UTC), số đợt phản hồi, số bài, lý do dừng, lỗi nếu có và danh sách bài viết. Mỗi bài có ID, tiêu đề, URL, thời gian đăng UTC, tên nguồn và mô tả.

## Giới hạn

Kết quả phụ thuộc vào các đợt dữ liệu mà trang Báo Mới tải khi cuộn. Trường `reached_article_older_than_window` bằng `false` nghĩa là script chưa gặp bài cũ hơn 24 giờ; khi đó không thể kết luận đã thu thập đủ bài trong khoảng thời gian này. Script dừng sau tối đa 10 phút, khi trang báo hết dữ liệu, hoặc sau 10 lượt chờ không nhận phản hồi mới.

Trang web và API có thể thay đổi, làm script cần cập nhật. Hãy sử dụng dữ liệu theo điều khoản của Báo Mới và nguồn xuất bản.
