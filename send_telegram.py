"""
Format danh sách bài viết đã tóm tắt thành một tin nhắn digest và gửi
qua Telegram Bot API (sendMessage).

Cố tình KHÔNG dùng parse_mode Markdown: tiêu đề bài viết lấy từ RSS có thể
chứa ký tự đặc biệt (_, *, [, ]...) khiến Telegram trả lỗi 400 "can't parse
entities". Gửi plain text vẫn được Telegram tự nhận diện link để bấm được.
"""
import os

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MIN_RELEVANCE = 2  # bỏ bớt tin bị chấm điểm liên quan quá thấp
MAX_DAILY_ITEMS = 10  # tổng số tin tối đa mỗi ngày
TELEGRAM_MAX_LEN = 3900  # để dư so với giới hạn cứng 4096 ký tự của Telegram

# Quota TỐI THIỂU cho từng category, đảm bảo không bị category nhiều nguồn
# hơn (hiện tại "embedded" có 10/15 nguồn) lấn át hoàn toàn trong top N.
# Category không có trong dict này (vd "embedded") không bị giới hạn — vẫn
# cạnh tranh bình thường ở phần "lấp đầy" bên dưới, thường sẽ chiếm phần lớn
# slot còn lại đúng theo tỷ lệ nguồn tự nhiên.
MIN_PER_CATEGORY = {
    "ran": 2,
    "research": 2,
    "virt": 2,
}

CATEGORY_LABEL = {
    "general": "Tech tổng quát",
    "ai-ml": "AI/ML",
    "embedded": "Embedded/Linux Kernel",
    "ran": "5G/5G-A/6G RAN",
    "research": "Kiến trúc/Mạng máy tính (paper)",
    "virt": "Ảo hóa (vRAN)",
}
CATEGORY_ORDER = ["ran", "virt", "embedded", "research", "ai-ml", "general"]


def _select_top_entries(filtered: list[dict]) -> list[dict]:
    """Chọn tối đa MAX_DAILY_ITEMS bài, đảm bảo quota tối thiểu mỗi category.

    Bước 1: với mỗi category có quota trong MIN_PER_CATEGORY, lấy tối đa
    `min_count` bài điểm cao nhất của riêng category đó — đây là các slot
    "được bảo đảm", không bị category khác giành mất.
    Bước 2: lấp đầy các slot còn lại bằng bài điểm cao nhất TOÀN CỤC
    (không phân biệt category) trong số bài chưa được chọn.
    """
    filtered.sort(key=lambda e: e.get("relevance", 3), reverse=True)

    selected: list[dict] = []
    selected_links: set[str] = set()

    for category, min_count in MIN_PER_CATEGORY.items():
        cat_entries = [e for e in filtered if e["category"] == category]
        for e in cat_entries[:min_count]:
            if e["link"] not in selected_links:
                selected.append(e)
                selected_links.add(e["link"])

    for e in filtered:
        if len(selected) >= MAX_DAILY_ITEMS:
            break
        if e["link"] not in selected_links:
            selected.append(e)
            selected_links.add(e["link"])

    selected.sort(key=lambda e: e.get("relevance", 3), reverse=True)
    return selected[:MAX_DAILY_ITEMS]


def build_digest(entries: list[dict]) -> str:
    filtered = [e for e in entries if e.get("relevance", 3) >= MIN_RELEVANCE]
    if not filtered:
        return "Hôm nay không có tin mới đáng chú ý."

    top = _select_top_entries(filtered)

    # Group để hiển thị theo category; vì `top` đã sort theo relevance,
    # thứ tự trong từng group cũng tự động đúng, không cần sort lại.
    grouped: dict[str, list[dict]] = {}
    for e in top:
        grouped.setdefault(e["category"], []).append(e)

    lines = ["Tech trend digest hôm nay"]
    ordered_categories = [c for c in CATEGORY_ORDER if c in grouped]
    ordered_categories += [c for c in grouped if c not in CATEGORY_ORDER]

    for category in ordered_categories:
        label = CATEGORY_LABEL.get(category, category)
        lines.append(f"\n== {label} ==")
        for e in grouped[category]:
            # Relevance badge: chỉ hiển thị khi điểm cao (để nổi bật tin quan trọng)
            badge = " 🔥" if e.get("relevance", 3) >= 5 else ""
            lines.append(f"• {e['title']}{badge}")
            lines.append(f"  {e['summary_vi']}")
            lines.append(f"  🔗 {e['link']}")  # icon để link dễ nhận ra trong plain text

    return "\n".join(lines)


def chunk_text(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Chia text thành nhiều đoạn theo ranh giới dòng, mỗi đoạn <= limit ký tự."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.split("\n"):
        added_len = len(line) + 1  # +1 cho ký tự xuống dòng
        if current and current_len + added_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += added_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def send_digest(entries: list[dict]) -> None:
    text = build_digest(entries)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    chunks = chunk_text(text)
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