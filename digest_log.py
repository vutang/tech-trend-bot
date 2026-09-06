"""
Ghi log mỗi lần chạy digest — lớp observability của pipeline.

Vì sao cần dù đã có state.json: state.json trả lời "bot cần nhớ gì để chạy
đúng lần sau", chỉ lưu {id: timestamp}, tự xoá sau 30 ngày. Log trả lời
"bot đã làm gì và kết quả ra sao" — cần source, category, điểm số, phiên
bản prompt/model để về sau đo được tác động của từng thay đổi.

Thiết kế:
- JSONL chia theo tháng (logs/YYYY-MM.jsonl). Append-only nên git diff
  luôn là thêm dòng, không rewrite cả file — tránh đúng kiểu thay đổi
  từng gây conflict rebase với seen.json.
- Ghi MỌI candidate, không chỉ bài được gửi. Bài bị rejected mới là dữ
  liệu quan trọng để đánh giá nguồn nhiễu. Bài hết hạn pending (status
  "expired") được ghi RIÊNG qua log_expired(), không lẫn vào "rejected"
  — hết hạn vì thời gian và bị loại vì nội dung kém là hai lý do khác
  nhau, gộp chung sẽ làm sai chỉ số noise_rate khi phân tích.
- Mỗi dòng TỰ CHỨA đủ thông tin (kể cả version), không cần join với dòng
  header. Tốn thêm ~150 byte/dòng nhưng đổi lại không bao giờ có dòng mồ
  côi khi file bị cắt/lỗi.
- KHÔNG lưu full article text: vấn đề bản quyền, và cũng không cần cho
  mục đích phân tích. Chỉ lưu metadata + summary đã sinh.
- Fail-safe: mọi lỗi ghi log đều bị nuốt và chỉ in cảnh báo. Log là dữ
  liệu phụ trợ, không bao giờ được phép làm hỏng digest.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
TITLE_MAX_LEN = 200
SUMMARY_MAX_LEN = 500

# Tăng số này khi đổi CẤU TRÚC bản ghi (thêm/bớt/đổi ý nghĩa trường) — không
# tăng khi chỉ đổi giá trị (model mới, prompt mới) vì đã có `versions` lo
# việc đó. analyze.py dùng trường này để biết cách đọc bản ghi cũ an toàn
# khi schema thay đổi về sau (ví dụ thêm "extraction" ở Phase 2).
SCHEMA_VERSION = 2


def _hash8(text: str) -> str:
    """Băm ngắn để định danh phiên bản prompt.

    Dùng hash thay vì số version gán tay ("filter-v3") vì gán tay chắc
    chắn sẽ có lúc quên tăng — và lúc đó trường version còn tệ hơn không
    có, vì ta tin nhầm rằng prompt không đổi. Hash thì không thể quên.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def pipeline_versions() -> dict:
    """Thu thập định danh phiên bản của mọi thành phần ảnh hưởng tới kết quả.

    Không có mấy trường này thì vài tuần sau nhìn thấy điểm trung bình
    thay đổi cũng không biết do đổi prompt, đổi model, thêm nguồn hay
    đổi quota.
    """
    versions = {"git_sha": os.environ.get("GITHUB_SHA", "")[:8]}
    try:
        import summarize as s
        versions.update({
            "filter_model": s.GEMINI_FILTER_MODEL,
            "summary_model": s.GEMINI_SUMMARIZE_MODEL,
            "deep_model": s.CLAUDE_MODEL,
            "filter_prompt": _hash8(s.GEMINI_FILTER_PROMPT),
            "summary_prompt": _hash8(s.GEMINI_SUMMARIZE_PROMPT),
            "deep_prompt": _hash8(s.CLAUDE_DEEP_PROMPT),
        })
    except Exception as exc:
        print(f"[log] Không đọc được version từ summarize.py: {exc!r}")
    return versions


def log_run(candidates: list[dict], delivered: list[dict],
            min_relevance: int) -> None:
    """Ghi một dòng cho mỗi candidate của lần chạy này.

    status:
      delivered — đã gửi, kèm digest_rank = vị trí trong digest (1 = đầu)
      rejected  — điểm dưới ngưỡng, loại hẳn
      pending   — đủ điểm nhưng chưa lọt top, sẽ xét lại ngày sau

    LƯU Ý cho bên phân tích: một bài ở trạng thái pending sẽ được ghi LẠI
    ở mỗi lần chạy cho tới khi được gửi hoặc hết hạn. Ngoài ra nếu gửi
    Telegram lỗi giữa chừng, state không được cập nhật nên lần sau bài đó
    được xử lý và ghi log lần nữa. Vì vậy analyze.py BẮT BUỘC phải dedupe
    theo article_id, nếu không sẽ đếm trùng.
    """
    try:
        LOG_DIR.mkdir(exist_ok=True)
        now = datetime.now(timezone.utc)
        run_id = now.isoformat()
        log_file = LOG_DIR / f"{now:%Y-%m}.jsonl"
        versions = pipeline_versions()

        rank_by_id = {e.get("id"): i + 1 for i, e in enumerate(delivered)}

        lines = []
        for e in candidates:
            article_id = e.get("id")
            if not article_id:
                continue

            if article_id in rank_by_id:
                status = "delivered"
            elif e.get("relevance", 3) < min_relevance:
                status = "rejected"
            else:
                status = "pending"

            record = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "article_id": article_id,
                "title": (e.get("title") or "")[:TITLE_MAX_LEN],
                "url": e.get("link", ""),
                "source": e.get("source", "?"),
                "category": e.get("category", "?"),
                "relevance": e.get("relevance"),
                "min_relevance_threshold": min_relevance,
                "status": status,
                "digest_rank": rank_by_id.get(article_id),
                "carry_days": e.get("carry_days", 0),
                "rss_summary_chars": len(e.get("summary") or ""),
                "versions": versions,
            }
            # summary = tóm tắt AI (chỉ có ở bài đã qua Bước 2/3).
            # raw_snippet = trích RSS gốc, LUÔN CÓ bất kể status — để
            # rate.py luôn hiển thị được một đoạn text nào đó cho MỌI bài.
            # Nếu chỉ bài delivered có "summary" còn rejected thì không có
            # gì, sự vắng mặt đó tự nó tiết lộ trạng thái, phá hỏng mục
            # đích trộn ngẫu nhiên bài rejected vào để dò false negative.
            if e.get("summary_vi"):
                record["summary"] = e["summary_vi"][:SUMMARY_MAX_LEN]
            record["raw_snippet"] = (e.get("summary") or "")[:SUMMARY_MAX_LEN]

            lines.append(json.dumps(record, ensure_ascii=False))

        if lines:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print(f"[log] Ghi {len(lines)} bản ghi vào {log_file.name}")

    except Exception as exc:
        # Nuốt lỗi có chủ đích — xem docstring đầu file.
        print(f"[log] Không ghi được log (bỏ qua, không ảnh hưởng digest): {exc!r}")


def log_expired(entries: list[dict]) -> None:
    """Ghi log riêng cho bài pending vừa hết hạn (status="expired").

    TÁCH RIÊNG khỏi log_run()/status="rejected" có chủ đích: hết hạn vì
    thời gian (PENDING_TTL_DAYS) và bị loại vì nội dung kém là hai lý do
    khác nhau. Gộp chung sẽ làm sai chỉ số noise_rate trong analyze.py —
    một nguồn có nhiều bài "hết hạn vì chờ quá lâu" (do quota category
    chật, không phải do nguồn kém) sẽ bị hiểu nhầm là nguồn nhiễu.
    """
    if not entries:
        return
    try:
        LOG_DIR.mkdir(exist_ok=True)
        now = datetime.now(timezone.utc)
        run_id = now.isoformat()
        log_file = LOG_DIR / f"{now:%Y-%m}.jsonl"
        versions = pipeline_versions()

        lines = []
        for e in entries:
            article_id = e.get("id")
            if not article_id:
                continue
            record = {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "article_id": article_id,
                "title": (e.get("title") or "")[:TITLE_MAX_LEN],
                "url": e.get("link", ""),
                "source": e.get("source", "?"),
                "category": e.get("category", "?"),
                "relevance": e.get("relevance"),
                "min_relevance_threshold": None,
                "status": "expired",
                "digest_rank": None,
                "carry_days": e.get("carry_days", 0),
                "rss_summary_chars": len(e.get("summary") or ""),
                "versions": versions,
            }
            if e.get("summary_vi"):
                record["summary"] = e["summary_vi"][:SUMMARY_MAX_LEN]
            record["raw_snippet"] = (e.get("summary") or "")[:SUMMARY_MAX_LEN]
            lines.append(json.dumps(record, ensure_ascii=False))

        if lines:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print(f"[log] Ghi {len(lines)} bản ghi expired vào {log_file.name}")

    except Exception as exc:
        print(f"[log] Không ghi được log expired (bỏ qua): {exc!r}")
