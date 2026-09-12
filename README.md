# Tech trend bot

Bot tổng hợp tin công nghệ hằng ngày cho kỹ sư embedded/firmware, Linux
kernel, 5G RAN, kiến trúc máy tính, networking và virtualization. Bot đọc các
nguồn RSS/Atom, dùng Gemini và Claude để lọc/tóm tắt, rồi gửi digest qua
Telegram. Production chạy bằng GitHub Actions, không cần server riêng.

## Pipeline

```text
load state
    ↓
fetch bài mới từ RSS/Atom
    ↓
Gemini chấm relevance 1–5
    ↓
Gemini tóm tắt các bài relevance >= 3
    ↓
Claude tóm tắt sâu hơn các bài relevance >= 5
    ↓
gộp với các bài pending từ lần chạy trước
    ↓
xếp hạng, bảo đảm độ phủ category và chọn tối đa 10 bài
    ↓
gửi Telegram, cập nhật state và ghi telemetry
```

Các model đang được cấu hình trong `summarize.py`:

- Filter: `gemini-3.5-flash-lite`.
- Tóm tắt thông thường: `gemini-3.5-flash`.
- Tóm tắt sâu: `claude-haiku-4-5-20251001`, chỉ áp dụng cho bài relevance 5.

Summary gửi tới Telegram hiện được sinh bằng tiếng Anh. Gemini được gọi qua
SDK `google-genai`; Claude được gọi qua SDK `anthropic`.

## Cài đặt

GitHub Actions dùng Python 3.12; nên dùng cùng phiên bản khi chạy local.

```bash
pip install -r requirements.txt
```

Các biến môi trường bắt buộc:

| Biến | Mục đích |
|---|---|
| `GEMINI_API_KEY` | Lọc relevance và tạo summary thông thường bằng Gemini |
| `ANTHROPIC_API_KEY` | Tạo summary sâu bằng Claude cho bài relevance 5 |
| `TELEGRAM_BOT_TOKEN` | Token bot lấy từ BotFather |
| `TELEGRAM_CHAT_ID` | Chat nhận digest |

Để lấy `TELEGRAM_CHAT_ID`, nhắn thử cho bot rồi gọi:

```text
https://api.telegram.org/bot<TOKEN>/getUpdates
```

## Chạy trên máy cá nhân

```bash
export GEMINI_API_KEY=...
export ANTHROPIC_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python main.py
```

Lệnh trên chạy pipeline thật, gửi Telegram, đồng thời cập nhật `state.json` và
log trong `logs/`.

## GitHub Actions

Workflow `Daily tech trend digest` trong
`.github/workflows/daily-digest.yml`:

- Chạy theo cron lúc `22:47 UTC` mỗi ngày, tương đương `05:47` ngày hôm sau
  theo giờ Việt Nam (UTC+7). GitHub Actions có thể khởi chạy trễ hơn lịch cron.
- Có `workflow_dispatch` để chạy thủ công từ tab **Actions**.
- Đọc bốn secret ở trên, chạy `python main.py`, rồi commit/push state và log
  được cập nhật.

Sau khi push repo lên GitHub, thêm cả bốn biến vào **Settings → Secrets and
variables → Actions**, rồi có thể chạy thử workflow từ tab **Actions** bằng
`workflow_dispatch`.

Repo còn có workflow chẩn đoán `Cron probe`, chạy mỗi 15 phút và chỉ in thời
gian UTC để theo dõi độ trễ scheduler.

## State và chống trùng

`state.json` là operational state chính, gồm ba nhóm:

- `delivered`: bài đã gửi.
- `pending`: bài đã được AI xử lý nhưng chưa lọt top 10; được xét lại mà không
  gọi AI lần nữa.
- `rejected`: bài dưới ngưỡng relevance hoặc bài pending đã hết hạn.

Pending được giữ tối đa 3 ngày. Khi xếp hạng, mỗi ngày pending bị trừ 0,5 điểm
để ưu tiên tin mới. Lịch sử delivered/rejected được giữ 30 ngày.

`seen.json` không còn là state chính. File này chỉ được đọc để migrate/union ID
từ định dạng state cũ nhằm giữ khả năng tương thích ngược.

Nếu gửi Telegram lỗi giữa chừng, pipeline không cập nhật state và không ghi
nhận các bài của lần chạy đó là đã gửi.

## Chọn bài cho digest

`send_telegram.py` chỉ xét bài relevance từ 3 và chọn tối đa 10 bài mỗi ngày.
Nếu có đủ ứng viên, các category sau được bảo đảm tối thiểu 2 bài:

- `ran`
- `research`
- `virt`

Các slot còn lại được lấp theo điểm xếp hạng toàn cục. Category `embedded`
không có quota riêng và vẫn cạnh tranh cho các slot này.

## Nguồn tin

Các nguồn production được khai báo trong `sources.yaml` và hiện chỉ dùng
RSS/Atom. Mỗi nguồn có:

```yaml
- name: Example source
  url: https://example.com/feed.xml
  category: embedded
```

Các category đang được cấu hình là `embedded`, `ran`, `research` và `virt`.
Với entry có timestamp, fetcher bỏ qua bài cũ hơn 26 giờ; mỗi nguồn được lấy
tối đa 5 bài hợp lệ trong một lần chạy.

## Observability và đánh giá

- `digest_log.py` ghi telemetry dạng JSONL vào `logs/YYYY-MM.jsonl`, gồm nguồn,
  category, relevance, trạng thái, digest rank và phiên bản model/prompt.
- `analyze.py` tổng hợp các chỉ số yield/noise/high-value dựa trên relevance do
  AI chấm, trạng thái pending/hết hạn và phân bổ slot:

  ```bash
  python analyze.py
  python analyze.py 2026-09
  ```

- `rate.py` cho phép chấm thủ công các bài đã log để đối chiếu đánh giá của AI:

  ```bash
  python rate.py
  python rate.py 30
  ```

## Cấu trúc repository

| File | Vai trò |
|---|---|
| `sources.yaml` | Cấu hình nguồn RSS/Atom |
| `fetch.py` | Thu thập, chuẩn hóa và loại các ID đã biết |
| `summarize.py` | Pipeline lọc/tóm tắt bằng Gemini và Claude |
| `send_telegram.py` | Xếp hạng, chọn, format và gửi digest |
| `state.py`, `state.json` | Quản lý operational state |
| `digest_log.py`, `logs/` | Ghi telemetry theo tháng |
| `analyze.py` | Phân tích telemetry offline |
| `rate.py` | Chấm điểm thủ công để tạo dữ liệu đánh giá |
| `main.py` | Entry point điều phối pipeline |
| `.github/workflows/daily-digest.yml` | Workflow production hằng ngày |
