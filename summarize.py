"""
Ba bước tóm tắt bài viết, ưu tiên Gemini — Claude chỉ cho tác vụ khó:

Bước 1 — Gemini Flash-Lite (rẻ nhất):
  Chấm relevance 1-5 cho toàn bộ bài mới.
  Loại bài dưới ngưỡng MIN_RELEVANCE_PREFILTER.

Bước 2 — Gemini Flash (chất lượng, vẫn rẻ hơn Claude):
  Tóm tắt tiếng Việt cho tất cả bài đã qua lọc.

Bước 3 — Claude Haiku (chỉ khi thực sự cần):
  Chỉ xử lý bài có relevance = CLAUDE_RELEVANCE_THRESHOLD (tin kỹ thuật sâu).
  Override summary_vi của Gemini bằng phân tích chất lượng cao hơn.
"""
import json
import os
import re

import anthropic
import google.generativeai as genai

# ── Models ────────────────────────────────────────────────────────────────────
GEMINI_FILTER_MODEL    = "gemini-2.5-flash-lite"    # bước 1: lọc — rẻ nhất
GEMINI_SUMMARIZE_MODEL = "gemini-2.5-flash"         # bước 2: tóm tắt — cân bằng
CLAUDE_MODEL           = "claude-haiku-4-5-20251001" # bước 3: kỹ thuật sâu — chỉ khi cần

# Bài có relevance >= ngưỡng này mới qua bước 2 (Gemini tóm tắt)
MIN_RELEVANCE_PREFILTER    = 3
# Bài có relevance >= ngưỡng này mới qua bước 3 (Claude override)
CLAUDE_RELEVANCE_THRESHOLD = 5
# Số bài tối đa mỗi lần gọi Claude, tránh hết token
CLAUDE_BATCH_SIZE          = 5

# ── Prompts ───────────────────────────────────────────────────────────────────
_JSON_RULE = (
    "Chỉ trả lời JSON hợp lệ, không kèm chữ nào khác, không dùng markdown code fence.\n"
    "QUAN TRỌNG: Phản hồi được nạp thẳng vào json.loads() trong Python."
)

# Bước 1: Gemini Flash-Lite — chỉ chấm điểm, không tóm tắt (ít token nhất)
GEMINI_FILTER_PROMPT = (
    "Bạn là bộ lọc tin tức công nghệ cho kỹ sư embedded/firmware mảng 5G RAN.\n\n"
    "Với danh sách bài viết JSON (mỗi bài có title/summary), chấm điểm relevance cho MỖI bài:\n"
    "- 5: Trực tiếp liên quan embedded, Linux kernel, firmware, 5G RAN, Open RAN, AI/ML\n"
    "- 4: Liên quan gián tiếp (chip, network, open source infra, telecom)\n"
    "- 3: Tin công nghệ tổng quát đáng chú ý\n"
    "- 2: Ít liên quan (business, funding, chính sách)\n"
    "- 1: PR/quảng cáo/không có nội dung kỹ thuật\n\n"
    'Dạng trả về: {"items": [{"title": "...", "relevance": 1-5}, ...]}\n'
    + _JSON_RULE
)

# Bước 2: Gemini Flash — tóm tắt tiếng Việt cho phần lớn bài
GEMINI_SUMMARIZE_PROMPT = (
    "Bạn là trợ lý tổng hợp tin tức công nghệ cho kỹ sư embedded/firmware mảng 5G RAN.\n\n"
    "Với danh sách bài viết JSON (mỗi bài có title/link/summary), với MỖI bài trả về:\n"
    "- title: giữ nguyên tiêu đề gốc\n"
    "- summary_vi: tóm tắt 1-2 câu tiếng Việt, nêu đúng thông tin chính, không nhận định chủ quan\n\n"
    'Dạng trả về: {"items": [{"title": "...", "summary_vi": "..."}, ...]}\n'
    + _JSON_RULE
)

# Bước 3: Claude Haiku — phân tích sâu cho bài kỹ thuật cao (relevance = 5)
CLAUDE_DEEP_PROMPT = (
    "Bạn là chuyên gia phân tích kỹ thuật cho kỹ sư embedded/firmware mảng 5G RAN.\n\n"
    "Các bài viết dưới đây được đánh giá là RẤT QUAN TRỌNG (relevance = 5).\n"
    "Với MỖI bài, hãy trả về:\n"
    "- title: giữ nguyên tiêu đề gốc\n"
    "- summary_vi: tóm tắt 2-3 câu tiếng Việt, nêu rõ: công nghệ cụ thể, "
    "tác động thực tế với kỹ sư embedded/5G, điểm đáng chú ý nhất\n\n"
    'Dạng trả về: {"items": [{"title": "...", "summary_vi": "..."}, ...]}\n'
    + _JSON_RULE
)


def _parse_json_safe(raw_text: str) -> dict | None:
    """Thử nhiều cách parse JSON từ text trả về của Claude.

    Trả về dict nếu thành công, None nếu thất bại hoàn toàn.
    """
    # Cách 1: Parse thẳng — trường hợp lý tưởng
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    # Cách 2: Loại bỏ markdown code fence (```json ... ``` hoặc ``` ... ```)
    stripped = re.sub(r"```(?:json)?\s*", "", raw_text).replace("```", "").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Cách 3: Tìm đoạn JSON đầu tiên bắt đầu bằng { và kết thúc bằng }
    # Dùng re.DOTALL để dấu . khớp cả ký tự xuống dòng
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass

    # Tất cả đều thất bại — log chi tiết để debug
    preview = raw_text[:200].replace("\n", "\\n")
    print(f"[cảnh báo] Claude không trả JSON hợp lệ, dùng tóm tắt gốc thay thế")
    print(f"[debug]    raw_text preview: {preview!r}")
    return None


def _gemini_call(model_name: str, prompt: str, payload: list[dict]) -> dict | None:
    """Helper gọi Gemini và parse JSON an toàn. Trả về dict hoặc None nếu lỗi."""
    model = genai.GenerativeModel(model_name)
    try:
        response = model.generate_content(
            prompt + "\n\n" + json.dumps(payload, ensure_ascii=False)
        )
        return _parse_json_safe(response.text.strip())
    except Exception as exc:
        print(f"[Gemini:{model_name}] Lỗi API: {exc}")
        return None


def _step1_filter(entries: list[dict]) -> list[dict]:
    """Bước 1 — Gemini Flash-Lite: chấm relevance, loại bài dưới ngưỡng.

    Fail-open: nếu Gemini lỗi → giữ toàn bộ để bước 2 xử lý.
    """
    payload = [
        {"title": e["title"], "summary": e["summary"][:300]}
        for e in entries
    ]
    parsed = _gemini_call(GEMINI_FILTER_MODEL, GEMINI_FILTER_PROMPT, payload)

    if parsed is None:
        print("[Bước 1] Gemini Filter lỗi — giữ toàn bộ bài, gán relevance mặc định")
        return [{**e, "relevance": MIN_RELEVANCE_PREFILTER} for e in entries]

    by_title = {item.get("title"): item for item in parsed.get("items", [])}
    scored = []
    for e in entries:
        relevance = by_title.get(e["title"], {}).get("relevance", MIN_RELEVANCE_PREFILTER)
        scored.append({**e, "relevance": relevance})

    passed = [e for e in scored if e["relevance"] >= MIN_RELEVANCE_PREFILTER]
    print(f"[Bước 1] {len(entries)} bài → lọc bỏ {len(entries)-len(passed)} → còn {len(passed)} bài")
    return passed


def _step2_summarize(entries: list[dict]) -> list[dict]:
    """Bước 2 — Gemini Flash: tóm tắt tiếng Việt cho toàn bộ bài đã lọc.

    Fail-open: nếu lỗi → giữ summary gốc tiếng Anh [:200].
    """
    payload = [
        {"title": e["title"], "summary": e["summary"][:500]}
        for e in entries
    ]
    parsed = _gemini_call(GEMINI_SUMMARIZE_MODEL, GEMINI_SUMMARIZE_PROMPT, payload)

    if parsed is None:
        print("[Bước 2] Gemini Summarize lỗi — dùng summary gốc")
        return [{**e, "summary_vi": e["summary"][:200]} for e in entries]

    by_title = {item.get("title"): item for item in parsed.get("items", [])}
    results = []
    for e in entries:
        summary_vi = by_title.get(e["title"], {}).get("summary_vi", e["summary"][:200])
        results.append({**e, "summary_vi": summary_vi})

    print(f"[Bước 2] Gemini tóm tắt xong {len(results)} bài")
    return results


def _step3_claude_deep(entries: list[dict]) -> list[dict]:
    """Bước 3 — Claude Haiku: phân tích sâu, chỉ override bài relevance = 5.

    Các bài còn lại giữ nguyên summary_vi từ Gemini.
    """
    hard = [e for e in entries if e.get("relevance", 0) >= CLAUDE_RELEVANCE_THRESHOLD]
    if not hard:
        print("[Bước 3] Không có bài relevance=5, bỏ qua Claude")
        return entries

    print(f"[Bước 3] Claude xử lý {len(hard)} bài kỹ thuật sâu (relevance=5)")
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # Kết quả Claude theo title để override
    claude_by_title: dict[str, str] = {}
    for i in range(0, len(hard), CLAUDE_BATCH_SIZE):
        batch = hard[i : i + CLAUDE_BATCH_SIZE]
        payload = [
            {"title": e["title"], "link": e["link"], "summary": e["summary"][:500]}
            for e in batch
        ]
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=CLAUDE_DEEP_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        )
        if response.stop_reason == "max_tokens":
            print(f"[Bước 3] Claude bị cắt token ở batch {i//CLAUDE_BATCH_SIZE+1}, giữ summary Gemini")
            continue

        parsed = _parse_json_safe(response.content[0].text.strip())
        if parsed:
            for item in parsed.get("items", []):
                if item.get("title") and item.get("summary_vi"):
                    claude_by_title[item["title"]] = item["summary_vi"]

    # Merge: ưu tiên Claude cho bài relevance=5, giữ Gemini cho phần còn lại
    results = []
    for e in entries:
        if e.get("relevance", 0) >= CLAUDE_RELEVANCE_THRESHOLD and e["title"] in claude_by_title:
            results.append({**e, "summary_vi": claude_by_title[e["title"]]})
        else:
            results.append(e)
    return results


def summarize_entries(entries: list[dict]) -> list[dict]:
    """Pipeline 3 bước: Gemini lọc → Gemini tóm tắt → Claude chỉ bài khó."""
    if not entries:
        return []

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    # Bước 1: Gemini Flash-Lite — lọc relevance (rẻ nhất)
    filtered = _step1_filter(entries)
    if not filtered:
        print("[pipeline] Không có bài nào qua lọc.")
        return []

    # Bước 2: Gemini Flash — tóm tắt tiếng Việt (phần lớn công việc)
    summarized = _step2_summarize(filtered)

    # Bước 3: Claude Haiku — chỉ override bài relevance=5 (kỹ thuật sâu)
    return _step3_claude_deep(summarized)
