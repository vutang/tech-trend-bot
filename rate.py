"""
Chấm điểm thủ công các tin đã gửi — tạo ground truth cho việc đánh giá.

    python rate.py            # chấm tối đa 15 tin
    python rate.py 30         # chấm tối đa 30 tin

Vì sao cần: mọi chỉ số trong analyze.py đều dựa trên `relevance` do AI
chấm, tức đo "AI nghĩ gì", không phải "hữu ích thật với bạn". Không có
ground truth thì không thể biết prompt/nguồn/extraction có thực sự tốt
lên hay chỉ khác đi.

THANG 3 MỨC, gắn với HÀNH ĐỘNG chứ không phải "chất lượng":
    2 = đáng mở link đọc kỹ
    1 = đọc tiêu đề là đủ, không cần mở
    0 = không nên xuất hiện trong digest
    ? = KHÔNG ĐỦ THÔNG TIN để đánh giá (xem bên dưới)

Gắn với hành động thì tự nhất quán hơn nhiều so với chấm điểm trừu tượng,
vì chỉ cần nhớ mình đã làm gì. Thang 3 mức (không phải 10) để bảng đối
chiếu chéo với relevance (3 mức thực dùng: 3/4/5) đủ mẫu mỗi ô, và để
thao tác đủ nhanh mà duy trì được lâu dài.

VÌ SAO `?` PHẢI TÁCH RIÊNG, KHÔNG GỘP VÀO 0:
2/1/0 đo MỨC HỮU ÍCH của bài. `?` đo CHẤT LƯỢNG TRÌNH BÀY của digest —
hai trục khác nhau. Một bài rất hay nhưng summary tệ tới mức không hiểu
nổi thì lỗi ở summary, không phải ở bài hay nguồn. Ép nó thành 0 sẽ đổ
oan cho nguồn, và tệ hơn: xoá mất chính tín hiệu dùng để chứng minh
Phase 2 (full-text extraction + structured summary) có tác dụng hay
không. Tỷ lệ `?` trước/sau khi làm extraction là thước đo sạch nhất cho
việc đó. Nếu tỷ lệ `?` cao trên toàn bộ nguồn thì vấn đề nằm ở format
digest, không phải ở nguồn tin nào cả.

HAI QUYẾT ĐỊNH VỀ PHƯƠNG PHÁP:

1) Hiện title + một đoạn mô tả (summary AI nếu có, hoặc trích RSS gốc)
   — ẨN điểm relevance và trạng thái (delivered/rejected). Ẩn con số
   relevance vì thấy nó trước sẽ neo phán đoán, mất giá trị đối chứng.
   Hiện mô tả vì đó chính là thứ mức "?" cần đánh giá độ rõ ràng — nếu
   ẩn cả mô tả thì "?" chỉ còn đo độ rõ của tiêu đề, thứ Phase 2 không
   hề đụng tới.

   Mọi bài LUÔN có gì đó để hiện (summary AI hoặc raw_snippet RSS gốc)
   — cố tình không bao giờ hiện "(không có summary)": sự vắng mặt đó sẽ
   tự tiết lộ đây là bài bị AI loại (chỉ bài rejected mới thiếu summary
   AI), phá hỏng mục đích trộn ngẫu nhiên ở mục (2).

2) Có chèn ngẫu nhiên vài tin bị AI LOẠI (status=rejected). Đây là điểm
   quan trọng: nếu chỉ chấm tin đã gửi, dữ liệu về nguyên tắc không bao
   giờ phát hiện được lỗi loại nhầm (false negative) — prompt có thể đang
   loại oan cả một mảng nội dung mà mọi chỉ số vẫn đẹp. Tin loại và tin
   gửi hiển thị giống hệt nhau nên bạn không phân biệt được.
"""
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
RATINGS_FILE = LOG_DIR / "ratings.jsonl"

DEFAULT_BATCH = 15
REJECTED_SAMPLE = 4   # số tin bị loại chèn vào mỗi phiên để dò false negative


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def load_candidates() -> tuple[list[dict], list[dict]]:
    """Trả về (tin đã gửi chưa chấm, tin bị loại chưa chấm)."""
    records = []
    for f in sorted(LOG_DIR.glob("*.jsonl")):
        if f.name == "ratings.jsonl":
            continue
        records.extend(_load_jsonl(f))

    # Giữ bản ghi cuối mỗi article_id (bài pending được ghi lại mỗi lần chạy)
    latest: dict[str, dict] = {}
    for r in sorted(records, key=lambda x: x.get("run_id", "")):
        if r.get("article_id"):
            latest[r["article_id"]] = r

    rated = {r["article_id"] for r in _load_jsonl(RATINGS_FILE)}
    delivered, rejected = [], []
    for r in latest.values():
        if r["article_id"] in rated:
            continue
        if r["status"] == "delivered":
            delivered.append(r)
        elif r["status"] == "rejected":
            rejected.append(r)

    delivered.sort(key=lambda r: r.get("run_id", ""), reverse=True)
    return delivered, rejected


def rate(batch: int = DEFAULT_BATCH) -> None:
    delivered, rejected = load_candidates()
    if not delivered and not rejected:
        print("Không còn tin nào chưa chấm. (Hoặc chưa có log — bot chưa chạy?)")
        return

    n_rejected = min(REJECTED_SAMPLE, len(rejected), max(batch // 4, 1))
    items = delivered[: batch - n_rejected] + random.sample(rejected, n_rejected)
    random.shuffle(items)  # trộn để không đoán được tin nào bị AI loại

    print(f"\nChấm {len(items)} tin. Thang điểm:")
    print("  2 = đáng mở link đọc kỹ")
    print("  1 = đọc tiêu đề là đủ")
    print("  0 = không nên xuất hiện trong digest")
    print("  ? = đọc mà không nắm được nội dung là gì")
    print("  s = bỏ qua tin này, q = thoát và lưu\n")

    new_ratings = []
    for i, item in enumerate(items, 1):
        print("=" * 70)
        print(f"[{i}/{len(items)}] {item['title']}")
        print(f"  {item.get('source', '?')} | {item.get('category', '?')}")
        # summary (AI) nếu có, không thì raw_snippet (RSS gốc) — CỐ TÌNH
        # không bao giờ hiện "(không có summary)": sự vắng mặt đó sẽ tự
        # tiết lộ đây là bài bị AI loại, phá hỏng việc trộn ngẫu nhiên.
        text = item.get("summary") or item.get("raw_snippet") or "(không có mô tả)"
        print(f"  {text}")
        print(f"  {item.get('url', '')}")
        while True:
            ans = input("  Điểm (2/1/0/?/s/q): ").strip().lower()
            if ans in ("2", "1", "0", "?"):
                record = {
                    "rated_at": datetime.now(timezone.utc).isoformat(),
                    "article_id": item["article_id"],
                    # rating=None khi `?`: KHÔNG phải điểm 0, mà là "chưa
                    # đánh giá được". Bên phân tích phải loại các bản ghi
                    # này khỏi thống kê mức hữu ích, và đếm riêng thành
                    # tỷ lệ unclear.
                    "rating": None if ans == "?" else int(ans),
                    "unclear": ans == "?",
                    # lưu lại để phân tích sau, KHÔNG hiển thị lúc chấm
                    "ai_status": item["status"],
                    "ai_relevance": item.get("relevance"),
                    "source": item.get("source"),
                    "category": item.get("category"),
                }
                new_ratings.append(record)
                break
            if ans == "s":
                break
            if ans == "q":
                _save(new_ratings)
                return
            print("  Nhập 2, 1, 0, ?, s hoặc q.")

    _save(new_ratings)


def _save(ratings: list[dict]) -> None:
    if not ratings:
        print("\nKhông có đánh giá nào để lưu.")
        return
    LOG_DIR.mkdir(exist_ok=True)
    with open(RATINGS_FILE, "a", encoding="utf-8") as f:
        for r in ratings:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nĐã lưu {len(ratings)} đánh giá vào {RATINGS_FILE.name}")

    # Tỷ lệ "không hiểu được" — thước đo chất lượng trình bày, sẽ là KPI
    # để đánh giá Phase 2 (extraction + structured summary) có tác dụng
    # thật hay không. So sánh con số này trước/sau khi làm Phase 2.
    unclear = [r for r in ratings if r.get("unclear")]
    if unclear:
        print(f"   Trong đó {len(unclear)}/{len(ratings)} tin không nắm được "
              f"nội dung ({len(unclear)/len(ratings)*100:.0f}%) — theo dõi con "
              f"số này trước/sau khi làm full-text extraction.")

    # Tiết lộ kết quả SAU khi chấm xong — tin bị AI loại mà bạn cho điểm cao
    # là tín hiệu ngưỡng lọc đang quá gắt.
    missed = [r for r in ratings
              if r["ai_status"] == "rejected" and (r["rating"] or 0) >= 2]
    if missed:
        print(f"\n⚠  {len(missed)} tin bạn cho 2 điểm nhưng AI đã LOẠI:")
        for r in missed:
            print(f"   - [{r['source']}] article_id={r['article_id'][:60]}")
        print("   Nếu lặp lại nhiều lần, ngưỡng lọc Bước 1 đang quá gắt.")


if __name__ == "__main__":
    rate(int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_BATCH)
