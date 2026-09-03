"""
Thu thập bài viết mới từ các nguồn RSS khai báo trong sources.yaml,
loại bỏ bài đã gửi trước đó dựa trên seen.json.
"""
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path

import feedparser
import yaml

SOURCES_FILE = Path(__file__).parent / "sources.yaml"
SEEN_FILE = Path(__file__).parent / "seen.json"
MAX_AGE_HOURS = 26  # rộng hơn 24h một chút để tránh lọt bài do lệch giờ cron
MAX_SEEN_IDS = 2000  # giữ tối đa từng này id để seen.json không phình to
MAX_PER_SOURCE = 3   # giới hạn số bài mỗi nguồn, tránh 1 nguồn chiếm hết slot


class _HTMLStripper(HTMLParser):
    """HTMLParser đơn giản: loại bỏ thẻ HTML, giữ lại text thuần."""
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        # Ghép các đoạn text, chuẩn hoá khoảng trắng thừa
        return re.sub(r"[ \t]+", " ", " ".join(self._parts)).strip()


def _strip_html(raw: str) -> str:
    """Loại bỏ thẻ HTML khỏi chuỗi, trả về plain text."""
    if not raw:
        return ""
    stripper = _HTMLStripper()
    stripper.feed(raw)
    return stripper.get_text()


def load_sources() -> list[dict]:
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)["sources"]


def load_seen() -> set:
    if not SEEN_FILE.exists():
        return set()
    with open(SEEN_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(seen: set) -> None:
    trimmed = list(seen)[-MAX_SEEN_IDS:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)


def fetch_new_entries() -> list[dict]:
    sources = load_sources()
    seen = load_seen()
    cutoff = time.time() - MAX_AGE_HOURS * 3600

    new_entries = []
    for source in sources:
        parsed = feedparser.parse(source["url"])
        if parsed.bozo and not parsed.entries:
            print(f"[cảnh báo] không đọc được feed: {source['name']} ({source['url']})")
            continue

        count = 0  # đếm số bài đã lấy từ nguồn này
        for entry in parsed.entries:
            if count >= MAX_PER_SOURCE:
                break

            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen:
                continue

            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and time.mktime(published) < cutoff:
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
            seen.add(entry_id)
            count += 1

    save_seen(seen)
    return new_entries


if __name__ == "__main__":
    entries = fetch_new_entries()
    print(f"Tìm thấy {len(entries)} bài mới")
    for e in entries:
        print(f"- [{e['category']}] {e['title']} ({e['source']})")
