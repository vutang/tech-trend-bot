"""
Format danh sách bài viết đã tóm tắt thành một tin nhắn digest và gửi
qua Telegram Bot API (sendMessage).

Cố tình KHÔNG dùng parse_mode Markdown: tiêu đề bài viết lấy từ RSS có thể
chứa ký tự đặc biệt (_, *, [, ]...) khiến Telegram trả lỗi 400 "can't parse
entities". Gửi plain text vẫn được Telegram tự nhận diện link để bấm được.
"""
import os
from datetime import datetime, timedelta, timezone

import requests

from state import AGE_PENALTY_PER_DAY
from digest_config import get_config

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MIN_RELEVANCE = 3  # PHẢI khớp MIN_RELEVANCE_PREFILTER bên summarize.py — bài
                    # dưới ngưỡng đó không có summary_vi (bị Bước 1 loại sớm,
                    # xem summarize.py), để lọt qua đây sẽ crash khi hiển thị
TELEGRAM_MAX_LEN = 3900  # leave room below Telegram's 4096 limit

CATEGORY_LABEL = {
    "general": "General Tech",
    "ai-ml": "AI/ML",
    "embedded": "Embedded/Linux Kernel",
    "ran": "5G/5G-A/6G RAN",
    "research": "Computer Architecture/Networking (papers)",
    "virt": "Virtualization (vRAN)",
}
CATEGORY_ORDER = ["ran", "virt", "embedded", "research", "ai-ml", "general"]


def _rank_score(e: dict) -> float:
    """Điểm dùng để XẾP HẠNG — bằng relevance gốc trừ đi phạt theo số ngày
    bài nằm trong pending. Giữ `relevance` nguyên vẹn cho phần hiển thị,
    để bài tồn kho không bị mất badge 🔥 chỉ vì cũ.
    """
    return e.get("relevance", 3) - AGE_PENALTY_PER_DAY * e.get("carry_days", 0)


def _select_top_entries(filtered: list[dict], *, session: str = "morning",
                        cap: int | None = None, quotas=None) -> list[dict]:
    """Reserve category minima, then fill remaining slots by global ranking."""
    config = get_config(session, cap=cap, quotas=quotas)
    ranked = sorted(filtered, key=_rank_score, reverse=True)
    selected: list[dict] = []
    selected_links: set[str] = set()

    for category, min_count in config.quotas.items():
        count = 0
        for e in ranked:
            if count >= min_count:
                break
            if e["category"] == category and e["link"] not in selected_links:
                selected.append(e)
                selected_links.add(e["link"])
                count += 1

    for e in ranked:
        if len(selected) >= config.cap:
            break
        if e["link"] not in selected_links:
            selected.append(e)
            selected_links.add(e["link"])

    return sorted(selected, key=_rank_score, reverse=True)


def select_entries(entries: list[dict], *, session: str = "morning",
                   cap: int | None = None, quotas=None) -> list[dict]:
    """Only relevance-qualified articles compete, including cached pending."""
    config = get_config(session, cap=cap, quotas=quotas)
    filtered = [e for e in entries if e.get("relevance", 3) >= MIN_RELEVANCE]
    return _select_top_entries(filtered, session=config.session,
                               cap=config.cap, quotas=config.quotas)


def _build_blocks(entries: list[dict], session: str = "morning") -> list[str]:
    """Xây digest dưới dạng list các KHỐI KHÔNG ĐƯỢC TÁCH RỜI khi chia tin
    nhắn Telegram. Mỗi khối là: tiêu đề chung (đứng riêng), hoặc category
    header gộp chung với entry đầu tiên của nó (để header không bao giờ
    đứng bơ vơ cuối 1 tin nhắn), hoặc từng entry còn lại (title+summary+link
    luôn đi cùng nhau, không bao giờ bị cắt giữa chừng).
    """
    get_config(session)
    top = entries
    if not top:
        return ["No noteworthy news today."]

    grouped: dict[str, list[dict]] = {}
    for e in top:
        grouped.setdefault(e["category"], []).append(e)

    ordered_categories = [c for c in CATEGORY_ORDER if c in grouped]
    ordered_categories += [c for c in grouped if c not in CATEGORY_ORDER]

    timestamp = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M GMT+7")
    blocks = [f"Tech trend digest — {session.title()} — {timestamp}"]
    for category in ordered_categories:
        label = CATEGORY_LABEL.get(category, category)
        for i, e in enumerate(grouped[category]):
            badge = " 🔥" if e.get("relevance", 3) >= 5 else ""
            entry_text = f"• {e['title']}{badge}\n  {e['summary_vi']}\n  🔗 {e['link']}"
            if i == 0:
                blocks.append(f"\n== {label} ==\n{entry_text}")
            else:
                blocks.append(entry_text)
    return blocks


def build_digest(entries: list[dict], *, session: str = "morning",
                 cap: int | None = None, quotas=None) -> str:
    """Xem trước digest dạng 1 chuỗi từ danh sách bài THÔ (tự chọn bên trong),
    bỏ qua giới hạn độ dài Telegram. Dùng để test/preview.
    """
    return "\n".join(_build_blocks(
        select_entries(entries, session=session, cap=cap, quotas=quotas), session))


def chunk_digest(entries: list[dict], limit: int = TELEGRAM_MAX_LEN, *,
                 session: str = "morning") -> list[str]:
    """Chia digest thành nhiều tin nhắn Telegram theo RANH GIỚI KHỐI — không
    bao giờ cắt rời 1 khối (category header + entry đầu, hoặc từng entry
    riêng) ra làm hai tin nhắn khác nhau như cách chia theo dòng cũ.
    """
    blocks = _build_blocks(entries, session)
    if len(blocks) == 1:
        return blocks  # trường hợp "No noteworthy news today."

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for block in blocks:
        block_len = len(block) + 1  # +1 cho "\n" nối giữa các khối
        if current and current_len + block_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(block)
        current_len += block_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def send_digest(entries: list[dict], *, session: str = "morning",
                cap: int | None = None, quotas=None) -> list[dict]:
    """Chọn bài, gửi Telegram, TRẢ VỀ danh sách bài đã thực sự gửi."""
    selected = select_entries(entries, session=session, cap=cap, quotas=quotas)
    if not selected:
        return []
    chunks = chunk_digest(selected, session=session)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    for i, chunk in enumerate(chunks):
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            data={
                "chat_id": chat_id,
                "text": chunk,
                # Chỉ bật preview cho chunk đầu tiên (bài quan trọng nhất)
                # Các chunk sau tắt preview để tránh spam ảnh preview
                "disable_web_page_preview": i > 0,
            },
            timeout=15,
        )
        if not resp.ok:
            # In ra lý do cụ thể Telegram trả về (vd: "chat not found",
            # "message is too long"...) thay vì chỉ có mã lỗi HTTP chung chung.
            print(f"[lỗi Telegram] {resp.status_code}: {resp.text}")
        resp.raise_for_status()

    return selected
