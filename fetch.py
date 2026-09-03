"""
Thu thập bài viết mới từ các nguồn RSS khai báo trong sources.yaml,
loại bỏ bài đã gửi trước đó dựa trên seen.json.
"""
import json
import time
from pathlib import Path

import feedparser
import yaml

SOURCES_FILE = Path(__file__).parent / "sources.yaml"
SEEN_FILE = Path(__file__).parent / "seen.json"
MAX_AGE_HOURS = 26  # rộng hơn 24h một chút để tránh lọt bài do lệch giờ cron
MAX_SEEN_IDS = 2000  # giữ tối đa từng này id để seen.json không phình to


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

        for entry in parsed.entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen:
                continue

            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and time.mktime(published) < cutoff:
                continue

            new_entries.append(
                {
                    "id": entry_id,
                    "title": entry.get("title", "").strip(),
                    "link": entry.get("link", ""),
                    "summary": entry.get("summary", ""),
                    "source": source["name"],
                    "category": source.get("category", "general"),
                }
            )
            seen.add(entry_id)

    save_seen(seen)
    return new_entries


if __name__ == "__main__":
    entries = fetch_new_entries()
    print(f"Tìm thấy {len(entries)} bài mới")
    for e in entries:
        print(f"- [{e['category']}] {e['title']} ({e['source']})")
