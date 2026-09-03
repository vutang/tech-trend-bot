"""
Format danh sách bài viết đã tóm tắt thành một tin nhắn digest và gửi
qua Telegram Bot API (sendMessage).
"""
import os

import requests

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MIN_RELEVANCE = 2  # bỏ bớt tin bị chấm điểm liên quan quá thấp

CATEGORY_LABEL = {
    "general": "Tech tổng quát",
    "ai-ml": "AI/ML",
    "embedded": "Embedded/5G/RAN",
}
CATEGORY_ORDER = ["embedded", "ai-ml", "general"]


def build_digest(entries: list[dict]) -> str:
    filtered = [e for e in entries if e.get("relevance", 3) >= MIN_RELEVANCE]
    if not filtered:
        return "Hôm nay không có tin mới đáng chú ý."

    grouped: dict[str, list[dict]] = {}
    for e in filtered:
        grouped.setdefault(e["category"], []).append(e)
    for items in grouped.values():
        items.sort(key=lambda e: e.get("relevance", 3), reverse=True)

    lines = ["*Tech trend digest hôm nay*"]
    ordered_categories = [c for c in CATEGORY_ORDER if c in grouped]
    ordered_categories += [c for c in grouped if c not in CATEGORY_ORDER]

    for category in ordered_categories:
        label = CATEGORY_LABEL.get(category, category)
        lines.append(f"\n*{label}*")
        for e in grouped[category]:
            lines.append(f"• [{e['title']}]({e['link']})\n  {e['summary_vi']}")

    return "\n".join(lines)


def send_digest(entries: list[dict]) -> None:
    text = build_digest(entries)
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    resp = requests.post(
        TELEGRAM_API.format(token=token),
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    resp.raise_for_status()
