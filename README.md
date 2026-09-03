# Tech trend bot

Thu thập tin công nghệ (tech tổng quát + embedded/5G/AI) hằng ngày, tóm tắt
bằng Claude, gửi digest qua Telegram. Chạy hoàn toàn bằng GitHub Actions,
không cần server.

## Cài đặt

1. Push repo này lên GitHub.
2. Vào **Settings → Secrets and variables → Actions**, thêm 3 secrets:
   - `TELEGRAM_BOT_TOKEN` — lấy từ @BotFather
   - `TELEGRAM_CHAT_ID` — lấy bằng cách nhắn thử 1 tin cho bot rồi gọi
     `https://api.telegram.org/bot<TOKEN>/getUpdates`
   - `ANTHROPIC_API_KEY` — tạo tại console.anthropic.com
3. Vào tab **Actions**, chọn workflow "Daily tech trend digest" → **Run workflow**
   để test ngay, không cần đợi cron.
4. Kiểm tra Telegram — nếu nhận được digest và log không lỗi là xong. Sau đó
   bot tự chạy mỗi ngày lúc 7h sáng giờ Hà Nội.

## Chạy thử trên máy cá nhân (tuỳ chọn, để debug trước khi push)

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export ANTHROPIC_API_KEY=...
python main.py
```

## Thêm nguồn tin

Sửa `sources.yaml` — mỗi nguồn cần `name`, `url` (RSS feed), `category`
(`general` / `ai-ml` / `embedded`). Không cần đổi code ở các file `.py`.

## Cấu trúc

| File | Vai trò |
|---|---|
| `sources.yaml` | Danh sách nguồn RSS |
| `fetch.py` | Thu thập + lọc trùng theo `seen.json` |
| `summarize.py` | Tóm tắt & chấm điểm liên quan bằng Claude API |
| `send_telegram.py` | Format digest & gửi qua Bot API |
| `main.py` | Điểm chạy chính, nối 3 bước trên |
| `.github/workflows/daily-digest.yml` | Lịch chạy hằng ngày |
