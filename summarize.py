"""
Tóm tắt tiếng Việt và chấm điểm mức độ liên quan cho danh sách bài viết,
dùng Claude API. Gộp tất cả bài vào một lần gọi để tiết kiệm chi phí.
"""
import json
import os

import anthropic

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """Bạn là trợ lý tổng hợp tin tức công nghệ cho một kỹ sư \
embedded/firmware làm việc trong mảng 5G RAN.

Với danh sách bài viết được cung cấp (dạng JSON, mỗi bài có title/link/summary), \
với MỖI bài hãy trả về một object gồm:
- title: giữ nguyên tiêu đề gốc, dùng để đối chiếu ngược lại
- summary_vi: tóm tắt 1-2 câu bằng tiếng Việt, nêu đúng thông tin chính, \
không thêm nhận định chủ quan
- relevance: số nguyên 1-5 (5 = rất đáng đọc với kỹ sư embedded/5G/AI, \
3 = tin tổng quát bình thường, 1 = tin PR/quảng cáo/không có nội dung kỹ thuật)

Chỉ trả lời bằng JSON hợp lệ dạng {"items": [...]}, không kèm chữ nào khác, \
không dùng markdown code fence.
"""


def summarize_entries(entries: list[dict]) -> list[dict]:
    if not entries:
        return []

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    payload = [
        {"title": e["title"], "link": e["link"], "summary": e["summary"][:500]}
        for e in entries
    ]

    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )

    raw_text = response.content[0].text.strip()
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        print("[cảnh báo] Claude không trả JSON hợp lệ, dùng tóm tắt gốc thay thế")
        return [{**e, "summary_vi": e["summary"][:200], "relevance": 3} for e in entries]

    by_title = {item.get("title"): item for item in parsed.get("items", [])}
    results = []
    for e in entries:
        extra = by_title.get(e["title"], {})
        results.append(
            {
                **e,
                "summary_vi": extra.get("summary_vi", e["summary"][:200]),
                "relevance": extra.get("relevance", 3),
            }
        )
    return results
