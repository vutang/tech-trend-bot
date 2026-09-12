"""
Thu thập bài viết mới từ các nguồn RSS/Atom trong sources.yaml và loại các ID
đã có trong operational state do caller cung cấp.
"""
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path

import feedparser
import yaml

SOURCES_FILE = Path(__file__).parent / "sources.yaml"
MAX_AGE_HOURS = 26  # rộng hơn 24h một chút để tránh lọt bài do lệch giờ cron
MAX_PER_SOURCE = 5   # giới hạn số bài mỗi nguồn, tránh 1 nguồn chiếm hết slot
                      # (tăng từ 3 lên 5 sau khi log thật cho thấy Phoronix
                      # chạm đúng ngưỡng 3 lặp lại ở 5 ngày riêng biệt — dấu
                      # hiệu bị cắt bớt, không phải trùng hợp tự nhiên)


class _HTMLStripper(HTMLParser):
    """HTMLParser đơn giản: loại bỏ thẻ HTML, giữ lại text thuần."""

    def __init__(self) -> None:
        """Khởi tạo vùng đệm chứa các đoạn text được parser đọc tuần tự."""
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        """Gom text giữa các HTML tag vào vùng đệm, giữ nguyên thứ tự."""
        self._parts.append(data)

    def get_text(self) -> str:
        """Trả text đã gom và chuẩn hóa khoảng trắng ngang dư thừa."""
        return re.sub(r"[ \t]+", " ", " ".join(self._parts)).strip()


def _strip_html(raw: str) -> str:
    """Loại thẻ HTML khỏi `raw`; chuỗi rỗng được giữ thành chuỗi rỗng."""
    if not raw:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(raw)
    return stripper.get_text()


def load_sources() -> list[dict]:
    """Đọc `sources.yaml` và trả danh sách source đang được cấu hình."""
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def fetch_new_entries(known: set | None = None) -> list[dict]:
    """Thu thập và chuẩn hóa bài mới, bỏ qua các ID đã có trong `known`.

    Chỉ nhận tối đa `MAX_PER_SOURCE` entry đủ điều kiện trong mỗi feed và trả
    về list dict dùng cho pipeline phía sau. Hàm có network I/O, in log chẩn
    đoán feed/cap nhưng không tự ghi state; state.py/main.py xử lý persistence
    sau khi biết bài nào thực sự được gửi.
    """
    sources = load_sources()
    seen = set(known or ())
    cutoff = time.time() - MAX_AGE_HOURS * 3600

    new_entries = []
    for source in sources:
        parsed = feedparser.parse(source["url"])
        if parsed.bozo and not parsed.entries:
            print(f"[cảnh báo] không đọc được feed: {source['name']} ({source['url']})")
            continue

        # Chẩn đoán: feed có bài thật không, mới nhất là khi nào — để phân
        # biệt "feed chết/stale" với "có bài nhưng bị AI chấm điểm thấp".
        # Không đổi hành vi gì, chỉ thêm 1 dòng log.
        newest = None
        if parsed.entries:
            pub = parsed.entries[0].get("published_parsed") or parsed.entries[0].get("updated_parsed")
            if pub:
                newest = time.strftime("%Y-%m-%d %H:%M UTC", pub)
        print(f"[fetch] {source['name']}: {len(parsed.entries)} bài thô, mới nhất: {newest or 'không rõ'}")

        count = 0    # đếm số bài đã lấy từ nguồn này
        capped = 0   # đếm số bài ĐỦ ĐIỀU KIỆN (mới + tươi) nhưng bị cap
                      # chặn không lấy — trước đây các bài này biến mất
                      # hoàn toàn không dấu vết, giờ ít nhất biết số lượng
        for entry in parsed.entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen:
                continue

            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and time.mktime(published) < cutoff:
                continue

            # Entry đủ điều kiện (mới + tươi) — nhưng nếu đã đủ quota thì
            # chỉ đếm lại, KHÔNG break, để tiếp tục quét hết feed và biết
            # chính xác tổng số bài bị bỏ lỡ hôm đó.
            if count >= MAX_PER_SOURCE:
                capped += 1
                continue

            # kernel.org kdist.xml: link luôn trỏ về trang chủ, cần tự build URL changelog
            link = entry.get("link", "")
            title = entry.get("title", "").strip()
            if "kernel.org" in source["url"] and link == "https://www.kernel.org/":
                # title dạng "7.2.3: stable" hoặc "6.12.108: longterm"
                version = title.split(":")[0].strip()
                major = version.split(".")[0]
                link = f"https://cdn.kernel.org/pub/linux/kernel/v{major}.x/ChangeLog-{version}"

            # Strip HTML khỏi summary — một số feed (kernel.org, LF blog)
            # trả về HTML đầy đủ trong trường summary thay vì plain text
            raw_summary = (
                entry.get("summary")
                or entry.get("description")
                or ""
            )
            summary = _strip_html(raw_summary)

            new_entries.append(
                {
                    "id": entry_id,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": source["name"],
                    "category": source.get("category", "general"),
                }
            )
            seen.add(entry_id)  # tránh trùng trong cùng lần chạy
            count += 1

        if capped:
            print(f"[fetch] {source['name']}: BỊ CẮT {capped} bài đủ điều kiện "
                  f"do MAX_PER_SOURCE={MAX_PER_SOURCE} — cân nhắc tăng cap nếu lặp lại thường xuyên")

    return new_entries


if __name__ == "__main__":
    entries = fetch_new_entries()
    print(f"Tìm thấy {len(entries)} bài mới")
    for e in entries:
        print(f"- [{e['category']}] {e['title']} ({e['source']})")
