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
xếp hạng, bảo đảm quota theo phiên: sáng tối đa 10, chiều tối đa 5 bài
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
python main.py --session morning
python main.py --session afternoon
```

Mỗi lệnh trên chạy một phiên pipeline thật, gửi Telegram, đồng thời cập nhật
`state.json` và log trong `logs/`. `python main.py` vẫn tương đương
`python main.py --session morning` để tương thích cách gọi cũ.
Chạy tay áp dụng cap của phiên, dùng chung state, nhưng không có trần cứng
15 bài/ngày. Kiểm thử offline dùng lệnh unit test bên dưới.

## GitHub Actions

Workflow `Daily tech trend digest` trong
`.github/workflows/daily-digest.yml`:

| Phiên | Giờ Việt Nam (UTC+7) | Cron UTC | Cap | Quota ran / research / virt |
|---|---|---|---:|---|
| morning | 05:47 | `47 22 * * *` | 10 | 2 / 2 / 2 |
| afternoon | 17:47 | `47 10 * * *` | 5 | 1 / 1 / 1 |

- Scheduled run lấy phiên từ `github.event.schedule`, nên cron trễ vẫn chạy
  đúng phiên. Mốc 22:47 UTC là 05:47 ngày hôm sau tại Việt Nam.
- `workflow_dispatch` có input choice `session`, mặc định `morning`.
  Có thể chọn nhánh tính năng trong **Run workflow → Use workflow from** để
  test trước khi merge. Workflow checkout nhánh đã chọn và commit/push
  state/log trở lại đúng nhánh đó; scheduled run vẫn dùng `master`.
  Manual run gọi AI và gửi Telegram thật bằng secrets đang cấu hình. State
  giữa các nhánh độc lập, nên test có thể gửi lại bài đã được master gửi.
- Một concurrency group cố định `tech-trend-bot-production` dùng chung cho
  scheduled/manual run trên mọi nhánh, `cancel-in-progress: false`. Job
  checkout sau khi được chạy để lấy state mới nhất của nhánh đã chọn khi
  đã chờ run trước; scheduled run checkout rõ `master`.
  Concurrency mặc định chỉ giữ một run pending; run chờ có thể bị thay thế
  nếu dispatch dồn dập ([GitHub Docs](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency)).
- Sau checkout, workflow lưu `git rev-parse HEAD` vào `BOT_CODE_SHA`, giữ nguyên
  `GITHUB_SHA`. Chạy unit test offline trước pipeline; bốn API secret chỉ được
  truyền vào bước gửi thật, không truyền vào test.
- Sau gửi thành công (hoặc không chọn được bài), commit state/log rồi
  pull/rebase rồi push vào cùng nhánh vừa checkout (`master` cho scheduled run).
  Conflict/rejected push làm job thất bại; không reset hoặc bỏ state để ép push.
  Khi pipeline/persist/push lỗi, workflow cố lưu state/log thành recovery
  artifact trong 7 ngày để điều tra, không tự động khôi phục dữ liệu.

Thêm bốn secret vào **Settings → Secrets and variables → Actions** khi triển
khai. Hai run theo lịch gửi tối đa **15 bài/ngày Việt Nam**; đây không phải
trần cứng theo ngày cho các lần chạy thủ công, rerun hoặc retry.

Repo còn có workflow chẩn đoán `Cron probe`, chạy mỗi 15 phút và chỉ in thời
gian UTC để theo dõi độ trễ scheduler.

## State và chống trùng

`state.json` là operational state chính, gồm ba nhóm:

- `delivered`: bài đã gửi.
- `pending`: bài đã được AI xử lý nhưng chưa lọt cap của phiên; được xét lại mà không
  gọi AI lần nữa.
- `rejected`: bài dưới ngưỡng relevance hoặc bài pending đã hết hạn.

Pending được giữ tối đa 3 ngày. Khi xếp hạng, mỗi ngày pending bị trừ 0,5 điểm
để ưu tiên tin mới. Lịch sử delivered/rejected được giữ 30 ngày.

`seen.json` không còn là state chính. File này chỉ được đọc để migrate/union ID
từ định dạng state cũ nhằm giữ khả năng tương thích ngược.

Nếu gửi Telegram lỗi giữa chừng, pipeline không cập nhật state và không ghi
nhận các bài của lần chạy đó là đã gửi. Nếu chunk đầu đã tới Telegram rồi
chunk sau lỗi, retry có thể gửi trùng. Nếu Telegram đã nhận đủ nhưng lưu
`state.json` thất bại hoặc commit/push không lên được remote, lần sau cũng có
thể gửi lại. Việc ghi state hiện không phải transaction với Telegram và có
thể để lại file ghi dở khi lỗi I/O. Recovery artifact chỉ hỗ trợ xử lý thủ công,
không bảo đảm lưu được nếu runner dừng đột ngột. **Không bảo đảm exactly-once.**

## Chọn bài cho digest

`digest_config.py` là nơi duy nhất định nghĩa cap/quota hai phiên. Cấu hình
bất biến, được validate trước fetch/AI và tại selection: session hợp lệ, cap
nguyên dương, quota nguyên không âm, tổng quota không vượt cap; sáng còn ít
nhất 4 slot toàn cục, chiều còn ít nhất 2 slot toàn cục.

`send_telegram.py` chỉ xét bài relevance từ 3. Trước tiên lấy quota tối thiểu
của từng category theo bảng; sau đó lấp slot còn lại bằng ranking toàn cục,
bao gồm `embedded`. Category thiếu bài nhường slot về toàn cục. Link trùng
không tiêu tốn thêm slot. Không cố gửi đủ cap nếu thiếu bài phù hợp.

Ví dụ có 17 bài đủ relevance: sáng chọn tối đa 10, lưu 7 bài còn lại thành
pending. Chiều đọc state mới, loại ID delivered khỏi fetch; 7 pending cạnh
tranh với bài mới theo relevance trừ age penalty, chọn tối đa 5. Pending giữ
summary/điểm đã tính, không gọi AI lại. Cửa sổ fetch 27 giờ và cap 5 bài/nguồn/run
áp dụng cho cả hai phiên; TTL/age penalty giữ nguyên như trên.

Tiêu đề Telegram có nhãn `Morning`/`Afternoon`. Nếu không chọn được bài,
pipeline không gọi Telegram nhưng vẫn cập nhật rejected, dọn pending hết hạn
và in context/số bài của run; các bản ghi candidate/expired (nếu có) vẫn được log.
Preview `build_digest(entries, session="afternoon")` dùng cùng selection và
cấu hình như `send_digest`; cả hai cũng nhận keyword `cap` và `quotas` đã validate.

## Kiểm thử và review

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py digest_config.py send_telegram.py digest_log.py analyze.py tests
```

Bốn file `tests/test_selection.py`, `tests/test_sessions.py`,
`tests/test_digest_log.py`, `tests/test_analyze.py` dùng `unittest`/mock; AI,
RSS và Telegram được mock, persistence dùng thư mục tạm. Test workflow parse
YAML, kiểm tra cấu hình, `bash -n` các script, mô phỏng resolver và push lỗi
bằng git giả. Chúng không chạy GitHub Actions hoặc pipeline production.

Review diff nhánh tính năng với `master`, chạy các lệnh trên; chú ý các ca
sáng → chiều, gửi lỗi, log lỗi và state lỗi. Sau khi được duyệt, merge vào
`master` với state/log production mới nhất và để hai cron chạy theo lịch.
Để test nhánh trước khi merge, commit/push thay đổi workflow và test lên nhánh,
vào **Actions → Daily tech trend digest → Run workflow**, chọn nhánh trong
**Use workflow from** và chọn `session`. File workflow cần tồn tại trên nhánh
mặc định để GitHub cho phép dispatch; bản workflow trên nhánh đã chọn sẽ chạy.
Nếu cần manual run production, chọn `master`. Mọi manual run gửi thật và có
thể tăng tổng ngày vượt 15. Theo dõi job, commit state,
telemetry và xử lý recovery artifact thủ công khi persist/push thất bại.

## Nguồn tin

Các nguồn production được khai báo trong `sources.yaml` và hiện chỉ dùng
RSS/Atom. Mỗi nguồn có:

```yaml
- name: Example source
  url: https://example.com/feed.xml
  category: embedded
```

Các category đang được cấu hình là `embedded`, `ran`, `research` và `virt`.
Với entry có timestamp, fetcher bỏ qua bài cũ hơn 27 giờ; mỗi nguồn được lấy
tối đa 5 bài hợp lệ trong một lần chạy.

## Observability và đánh giá

- `digest_log.py` ghi telemetry dạng JSONL vào `logs/YYYY-MM.jsonl`, gồm nguồn,
  category, relevance, trạng thái, digest rank và phiên bản model/prompt.
  Schema 3 thêm session, cap/quota thực tế, `logged_at`; `main.py` sinh một
  `run_id` (timestamp + UUID) dùng chung cho candidate và expired. `BOT_CODE_SHA`
  được đối chiếu HEAD; local dùng HEAD đã kiểm tra. Code chưa commit hoặc SHA
  không xác minh được có `git_sha=null`, kèm nguồn/HEAD nền nếu biết.
  Lỗi log chỉ cảnh báo, không làm hỏng digest; log lịch sử không được migrate.
- `analyze.py` tổng hợp các chỉ số yield/noise/high-value dựa trên relevance do
  AI chấm, trạng thái pending/hết hạn và phân bổ slot. Chỉ đọc file tháng
  `YYYY-MM.jsonl`, loại `ratings.jsonl` kể cả rating không còn log gốc.
  Đếm run từ log gốc trước khi khử trùng lặp; kết cục mỗi `article_id` lấy bản
  cuối. Pending sáng rồi delivered chiều chỉ tính một bài, gửi vào chiều.
  `first_observed` lấy bản ghi đầu trong kỳ (không nhất thiết là lần fetch đầu
  nếu chỉ đọc một tháng). Log cũ thiếu session là `legacy/unknown`.
  Chưa có run-level record nên không suy ra run rỗng, run gửi lỗi hay mất log;
  schema cũ từng sinh ID riêng cho expired cũng không thể gộp chắc chắn.
  Báo cáo hiện không tổng hợp chất lượng từ rating thủ công.

  Chạy phân tích offline:

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
| `main.py` | Entry point điều phối pipeline và CLI session |
| `digest_config.py` | Cap/quota và validation chung của hai phiên |
| `tests/test_*.py` | Unit test offline cho selection, phiên, telemetry và phân tích |
| `.github/workflows/daily-digest.yml` | Workflow production hằng ngày |
