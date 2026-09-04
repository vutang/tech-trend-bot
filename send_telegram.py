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
    "general": "General Tech",
    "ai-ml": "AI/ML",
    "embedded": "Embedded/Linux Kernel",
    "ran": "5G/5G-A/6G RAN",
    "research": "Computer Architecture/Networking (papers)",
    "virt": "Virtualization (vRAN)",
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


def _build_blocks(entries: list[dict]) -> list[str]:
    """Xây digest dưới dạng list các KHỐI KHÔNG ĐƯỢC TÁCH RỜI khi chia tin
    nhắn Telegram. Mỗi khối là: tiêu đề chung (đứng riêng), hoặc category
    header gộp chung với entry đầu tiên của nó (để header không bao giờ
    đứng bơ vơ cuối 1 tin nhắn), hoặc từng entry còn lại (title+summary+link
    luôn đi cùng nhau, không bao giờ bị cắt giữa chừng).
    """
    filtered = [e for e in entries if e.get("relevance", 3) >= MIN_RELEVANCE]
    if not filtered:
        return ["No noteworthy news today."]

    top = _select_top_entries(filtered)

    grouped: dict[str, list[dict]] = {}
    for e in top:
        grouped.setdefault(e["category"], []).append(e)

    ordered_categories = [c for c in CATEGORY_ORDER if c in grouped]
    ordered_categories += [c for c in grouped if c not in CATEGORY_ORDER]

    blocks = ["Tech trend digest today"]
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


def build_digest(entries: list[dict]) -> str:
    """Ghép toàn bộ digest thành 1 chuỗi — dùng để xem trước/test, không
    quan tâm giới hạn độ dài Telegram (xem chunk_digest cho việc đó).
    """
    return "\n".join(_build_blocks(entries))


def chunk_digest(entries: list[dict], limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    """Chia digest thành nhiều tin nhắn Telegram theo RANH GIỚI KHỐI — không
    bao giờ cắt rời 1 khối (category header + entry đầu, hoặc từng entry
    riêng) ra làm hai tin nhắn khác nhau như cách chia theo dòng cũ.
    """
    blocks = _build_blocks(entries)
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


def send_digest(entries: list[dict]) -> None:
    chunks = chunk_digest(entries)
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