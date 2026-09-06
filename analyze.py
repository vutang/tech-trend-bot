"""
Phân tích log digest — CHẠY THỦ CÔNG, không nằm trong workflow hằng ngày.

    python analyze.py              # toàn bộ log có sẵn
    python analyze.py 2026-09      # chỉ tháng 9/2026

=============================================================================
ĐỌC KỸ TRƯỚC KHI TIN VÀO SỐ LIỆU
=============================================================================

1) Chỉ số KHÔNG bị nhiễu loạn (tính trên tổng số bài fetch được từ nguồn,
   độc lập với quota và giới hạn top-N):

     yield      = (delivered + pending) / fetched   -- tỷ lệ còn "sống"
     noise      = rejected / fetched                -- tỷ lệ bị loại VÌ NỘI DUNG
     expired    = expired / fetched                 -- tỷ lệ hết hạn VÌ THỜI GIAN
     high_value = (relevance >= 4) / fetched         -- tỷ lệ bài điểm cao

   `noise` và `expired` CỐ TÌNH tách riêng: một bài hết hạn vì quota
   category chật (không đủ chỗ trong PENDING_TTL_DAYS) không phải do
   nguồn kém — gộp chung sẽ đổ oan cho nguồn.

   Đây là nhóm dùng được để so sánh nguồn.

2) Chỉ số CHỈ ĐỂ THAM KHẢO (telemetry), KHÔNG phải thước đo chất lượng nguồn:

     delivery = delivered / eligible

   Bị bóp méo bởi MIN_PER_CATEGORY và MAX_DAILY_ITEMS. Ví dụ category `ran`
   chỉ có quota tối thiểu 2 slot: dù RCR Wireless ra 10 bài tốt, phần lớn
   vẫn không được gửi — thấp không có nghĩa là nguồn kém. Dùng chỉ số này
   để gán "quality" rồi cho ảnh hưởng ngược lên ranking sẽ tạo vòng lặp
   tự khẳng định: nguồn được ưu tiên -> gửi nhiều -> bị kết luận là tốt ->
   ưu tiên mạnh hơn.

3) Mọi chỉ số dựa trên `relevance` đều đo "AI nghĩ gì", không phải "hữu ích
   thật với bạn". Muốn có ground truth cần feedback người đọc.

4) So sánh nguồn NÊN thực hiện trong cùng category. arXiv (chục bài/ngày)
   và Firecracker (vài release/tháng) không so trực tiếp được.
=============================================================================
"""
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"

HIGH_VALUE_THRESHOLD = 4          # relevance >= mức này coi là bài giá trị cao
NOISY_RATE = 0.80                 # noise vượt mức này -> cảnh báo nguồn nhiễu
NOISY_MIN_VOLUME = 15             # nhưng cần đủ mẫu mới kết luận
DEAD_SOURCE_DAYS = 14             # im lặng ngần này ngày -> nghi feed hỏng
MIN_DAYS_FOR_SILENCE_WARNING = 14 # cần ngần này ngày log mới đủ căn cứ


def load_records(month: str | None = None) -> list[dict]:
    if not LOG_DIR.exists():
        print(f"Chưa có {LOG_DIR}/ — bot chưa chạy lần nào kể từ khi bật log.")
        return []

    files = sorted(LOG_DIR.glob(f"{month}.jsonl" if month else "*.jsonl"))
    if not files:
        print(f"Không tìm thấy file log nào khớp.")
        return []

    records = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    print(f"[cảnh báo] Bỏ qua dòng hỏng: {f.name}:{line_no}")
    return records


def dedupe(records: list[dict]) -> list[dict]:
    """Giữ bản ghi CUỐI CÙNG của mỗi article_id.

    Bắt buộc phải làm: một bài pending được ghi lại ở MỖI lần chạy cho tới
    khi được gửi hoặc hết hạn, nên không dedupe sẽ đếm trùng nhiều lần.
    Giữ bản cuối vì đó là kết cục thật của bài đó.
    """
    latest: dict[str, dict] = {}
    for r in sorted(records, key=lambda x: x.get("run_id", "")):
        aid = r.get("article_id")
        if aid:
            latest[aid] = r
    dropped = len(records) - len(latest)
    if dropped:
        print(f"(Đã gộp {dropped} bản ghi trùng article_id — bài pending "
              f"được ghi lại mỗi lần chạy.)\n")
    return list(latest.values())


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:3.0f}%" if whole else "  -"


def _source_stats(rs: list[dict]) -> dict:
    fetched = len(rs)
    delivered = sum(1 for r in rs if r["status"] == "delivered")
    pending = sum(1 for r in rs if r["status"] == "pending")
    rejected = sum(1 for r in rs if r["status"] == "rejected")
    expired = sum(1 for r in rs if r["status"] == "expired")
    high = sum(1 for r in rs
               if isinstance(r.get("relevance"), (int, float))
               and r["relevance"] >= HIGH_VALUE_THRESHOLD)
    rels = [r["relevance"] for r in rs if isinstance(r.get("relevance"), (int, float))]
    eligible = delivered + pending
    return {
        "fetched": fetched, "delivered": delivered, "pending": pending,
        "rejected": rejected, "expired": expired, "high": high,
        "eligible": eligible,
        "avg_rel": sum(rels) / len(rels) if rels else 0,
    }


def report(records: list[dict]) -> None:
    if not records:
        return
    records = dedupe(records)

    runs = {r.get("run_id") for r in records}
    days = sorted({r["run_id"][:10] for r in records if r.get("run_id")})
    status_count = defaultdict(int)
    for r in records:
        status_count[r["status"]] += 1

    print("=" * 74)
    print(f"TỔNG QUAN — {days[0]} → {days[-1]} ({len(days)} ngày, {len(runs)} lần chạy)")
    print("=" * 74)
    print(f"Bài (đã khử trùng lặp): {len(records)}"
          f" | đã gửi {status_count['delivered']}"
          f" | tồn kho {status_count['pending']}"
          f" | loại {status_count['rejected']}")

    # ---- Theo nguồn, NHÓM THEO CATEGORY ----
    by_cat_source = defaultdict(lambda: defaultdict(list))
    for r in records:
        by_cat_source[r["category"]][r["source"]].append(r)

    print("\n" + "=" * 74)
    print("CHỈ SỐ THEO NGUỒN — so sánh trong cùng category")
    print("yield/noise/high tính trên tổng bài fetch: KHÔNG bị quota bóp méo")
    print("delivery chỉ là telemetry, KHÔNG dùng làm thước đo chất lượng")
    print("=" * 74)

    for cat in sorted(by_cat_source):
        print(f"\n### {cat}")
        print(f"{'Nguồn':<32} {'Bài':>4} {'yield':>6} {'noise':>6} "
              f"{'exp':>5} {'high':>6} {'Rel.TB':>7} {'(deliv)':>8}")
        print("-" * 78)
        rows = [(src, _source_stats(rs)) for src, rs in by_cat_source[cat].items()]
        for src, s in sorted(rows, key=lambda x: -x[1]["high"] / max(x[1]["fetched"], 1)):
            print(f"{src[:32]:<32} {s['fetched']:>4} "
                  f"{_pct(s['eligible'], s['fetched']):>6} "
                  f"{_pct(s['rejected'], s['fetched']):>6} "
                  f"{_pct(s['expired'], s['fetched']):>5} "
                  f"{_pct(s['high'], s['fetched']):>6} "
                  f"{s['avg_rel']:>7.1f} "
                  f"{_pct(s['delivered'], s['eligible']):>8}")

    # ---- Cảnh báo ----
    warnings = []
    now = datetime.now(timezone.utc)
    all_sources = defaultdict(list)
    for r in records:
        all_sources[r["source"]].append(r)

    for src, rs in all_sources.items():
        s = _source_stats(rs)
        # Cảnh báo dựa trên noise (không nhiễu loạn), KHÔNG dùng delivery rate
        if s["fetched"] >= NOISY_MIN_VOLUME and s["rejected"] / s["fetched"] > NOISY_RATE:
            warnings.append(
                f"{src}: {s['rejected']}/{s['fetched']} bài bị loại thẳng "
                f"({s['rejected']/s['fetched']*100:.0f}%) — nhiễu cao")
        last = max(r["run_id"] for r in rs if r.get("run_id"))
        age = (now - datetime.fromisoformat(last)).days
        if age >= DEAD_SOURCE_DAYS:
            warnings.append(f"{src}: không có bài nào {age} ngày — kiểm tra feed")

    if len(days) >= MIN_DAYS_FOR_SILENCE_WARNING:
        try:
            import yaml
            cfg = yaml.safe_load(open(Path(__file__).parent / "sources.yaml"))
            never = {s["name"] for s in cfg["sources"]} - set(all_sources)
            for src in sorted(never):
                warnings.append(f"{src}: chưa từng có bài nào trong {len(days)} "
                                f"ngày — feed hỏng, hoặc ra tin rất thưa")
        except Exception as exc:
            print(f"\n[cảnh báo] Không đọc được sources.yaml: {exc!r}")
    else:
        print(f"\n(Bỏ qua kiểm tra nguồn im lặng — cần {MIN_DAYS_FOR_SILENCE_WARNING} "
              f"ngày log, hiện có {len(days)}.)")

    if warnings:
        print("\n" + "=" * 74)
        print("NGUỒN CẦN XEM LẠI")
        print("=" * 74)
        for w in warnings:
            print(f"  - {w}")

    # ---- Phân bổ slot theo category ----
    print("\n" + "=" * 74)
    print("PHÂN BỔ SLOT ĐÃ GỬI THEO CATEGORY")
    print("=" * 74)
    total_delivered = status_count["delivered"]
    cat_delivered = defaultdict(int)
    for r in records:
        if r["status"] == "delivered":
            cat_delivered[r["category"]] += 1
    for cat, n in sorted(cat_delivered.items(), key=lambda x: -x[1]):
        print(f"  {cat:<20} {n:>4}  {_pct(n, total_delivered)}")

    # ---- Version đang dùng ----
    versions = {json.dumps(r.get("versions", {}), sort_keys=True) for r in records}
    print("\n" + "=" * 74)
    print(f"PHIÊN BẢN PIPELINE ({len(versions)} cấu hình khác nhau trong kỳ)")
    print("=" * 74)
    if len(versions) > 1:
        print("  Có nhiều hơn 1 cấu hình — cẩn thận khi so sánh số liệu trước/sau,")
        print("  thay đổi có thể đến từ prompt/model chứ không phải từ nguồn tin.")
    for v in sorted(versions):
        d = json.loads(v)
        if d:
            print(f"  filter_prompt={d.get('filter_prompt')} "
                  f"summary_prompt={d.get('summary_prompt')} "
                  f"git={d.get('git_sha')}")

    print("\nLƯU Ý: chỉ số dựa trên điểm AI chấm phản ánh 'AI nghĩ gì', không phải")
    print("mức hữu ích thật. Xem docstring đầu file trước khi ra quyết định.")


if __name__ == "__main__":
    report(load_records(sys.argv[1] if len(sys.argv) > 1 else None))
