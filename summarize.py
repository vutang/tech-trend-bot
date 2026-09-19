"""
Ba bước tóm tắt bài viết, ưu tiên Gemini — Claude chỉ cho tác vụ khó:

Bước 1 — Gemini Flash-Lite (rẻ nhất):
  Chấm relevance 1-5 cho toàn bộ bài mới.
  Loại bài dưới ngưỡng MIN_RELEVANCE_PREFILTER.

Bước 2 — Gemini Flash (chất lượng, vẫn rẻ hơn Claude):
  Tạo tóm tắt tiếng Anh cho tất cả bài đã qua lọc.

Bước 3 — Claude Haiku (chỉ khi thực sự cần):
  Chỉ xử lý bài có relevance >= CLAUDE_RELEVANCE_THRESHOLD (tin kỹ thuật sâu).
  Ghi đè summary_vi của Gemini bằng phân tích chất lượng cao hơn.
"""
import json
import os
import re
import traceback

import anthropic
from google import genai
from google.genai import types

# ── Models ────────────────────────────────────────────────────────────────────
GEMINI_FILTER_MODEL    = "gemini-3.5-flash-lite"    # bước 1: lọc — rẻ nhất
GEMINI_SUMMARIZE_MODEL = "gemini-3.5-flash"         # bước 2: tóm tắt — cân bằng
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
    "SỞ THÍCH NGƯỜI ĐỌC:\n"
    "- Cốt lõi: embedded/firmware, Linux kernel, real-time, 5G RAN/Open RAN, "
    "kiến trúc máy tính, networking và virtualization.\n"
    "- Cũng quan tâm: công cụ và cách làm việc của lập trình viên, Git/hệ thống phân tán, "
    "lựa chọn model và tối ưu workflow AI, thực hành/debug phần cứng, quản lý dự án cá nhân, "
    "Raspberry Pi và ứng dụng self-hosted có thay đổi hữu ích. "
    "Không bắt buộc các chủ đề này phải liên quan 5G mới được giữ lại.\n\n"
    "Với danh sách bài viết JSON (mỗi bài có title/summary), chấm điểm relevance cho MỖI bài:\n"
    "- 5: Nội dung kỹ thuật trực tiếp thuộc nhóm cốt lõi, có thay đổi, cơ chế hoặc vấn đề "
    "cụ thể đáng tìm hiểu sâu: kernel/real-time, RAN, firmware/bootloader, chip/SoC, "
    "kiến trúc máy tính/mạng (nghiên cứu hoặc triển khai).\n"
    "- 4: Nội dung kỹ thuật cụ thể thuộc các sở thích trên: driver, compiler/toolchain, "
    "protocol, BSP/SDK, benchmark, thiết kế hệ thống, hướng dẫn/debug phần cứng.\n"
    "- 3: Có giá trị cập nhật hoặc thực hành cho người đọc nhưng chưa sâu kỹ thuật: "
    "trade-off công cụ/model AI, cách tổ chức công việc/dự án, thay đổi hữu ích ở "
    "Raspberry Pi/self-hosted, xu hướng công nghệ có phân tích.\n"
    "- 2: Release/ISO/snapshot định kỳ, cập nhật desktop chung chung không có thay đổi "
    "hữu ích được nêu; business/funding/chính sách; tin AI doanh nghiệp hoặc nghiên cứu "
    "ngoài sở thích, không có bài học kỹ thuật phù hợp trong đầu vào.\n"
    "- 1: PR/quảng cáo/giải thưởng, nội dung đã xoá hoặc không có thông tin hữu ích.\n\n"
    "RANH GIỚI:\n"
    "- Bài có điểm >= 3 được giữ lại. Chấm theo thông tin trong title/summary, "
    "không suy diễn ứng dụng 5G/embedded để nâng điểm.\n"
    "- Không tự nâng điểm chỉ vì có từ 'release', 'AI', 'Git', 'GPU', 'network' "
    "hoặc vì bài là nghiên cứu. Xét vấn đề, thay đổi và ích lợi cụ thể được nêu.\n"
    "- Bài hỏi kinh nghiệm thực hành/quản lý dự án có thể đạt 3 dù chưa có lời giải; "
    "không mặc định mọi thảo luận cộng đồng đều là nhiễu.\n\n"
    "VÍ DỤ HIỆU CHỈNH (điểm relevance 1-5, không phải điểm rating 0-2):\n"
    "- 'The Essence of Git: Concepts for P2P Replication': giải thích object model và "
    "replication để xây ứng dụng phân tán -> 4.\n"
    "- 'Jellyfin 12.0 released': có cải tiến database và nâng FFmpeg cho transcoding "
    "-> 3; không loại chỉ vì là ứng dụng end-user.\n"
    "- 'Smart enough, fast enough: Choosing the right models for agentic work': "
    "bàn trade-off độ chính xác và độ trễ trong workflow coding agent -> 3.\n"
    "- 'How do you manage multiple projects at once?': vướng phần cứng, cân nhắc "
    "đổi dự án và tránh kiệt sức khi làm dự án cá nhân -> 3.\n"
    "- 'Gitte As Git Client For GNOME Continues Maturing Quite Nicely': chỉ báo "
    "version mới của Git GUI, chưa nêu cải tiến hữu ích -> 2.\n"
    "- 'Build a compliance assistant with AutoRAG and Red Hat OpenShift AI': "
    "use case hỏi đáp tài liệu tuân thủ doanh nghiệp, chưa có bài học phù hợp "
    "với sở thích trên trong đoạn cung cấp -> 2.\n"
    "- 'Red Hat is named a Leader in IDC MarketScape: Worldwide Private and Hybrid "
    "Cloud Management with Automation': tin giải thưởng vendor -> 1.\n"
    "Ví dụ minh hoạ ranh giới, không phải danh sách title/nhãn hiệu được ưu tiên.\n\n"
    "Giữ nguyên title; trả đúng một kết quả cho mỗi bài đầu vào.\n"
    'Dạng trả về: {"items": [{"title": "...", "relevance": 1-5}, ...]}\n'
    + _JSON_RULE
)

# Dùng chung để Gemini và Claude giữ cùng tiêu chuẩn về độ rõ và căn cứ.
_SUMMARY_CONTENT_RULES = (
    "QUY TẮC NỘI DUNG:\n"
    "- Chỉ dùng thông tin trong title/summary được cung cấp. Link nếu có chỉ là "
    "định danh; không giả định đã đọc toàn văn.\n"
    "- Với bài nghiên cứu: diễn đạt theo thứ tự vấn đề cần giải quyết -> "
    "phương pháp/cơ chế chính -> kết quả hoặc giới hạn được nguồn nêu. "
    "Dùng câu văn liền mạch, không cần nhãn cho từng phần.\n"
    "- Giải thích ngắn vai trò của thuật ngữ quan trọng bằng ngôn ngữ dễ hiểu "
    "cho kỹ sư chưa chuyên về đề tài đó; tránh chỉ lặp lại tên phương pháp/viết tắt.\n"
    "- Giữ số liệu, điều kiện đo và giới hạn khi có trong đầu vào. Nếu đoạn trích "
    "thiếu kết quả thì chỉ tóm tắt vấn đề/phương pháp đã biết; không bịa số liệu, "
    "benchmark hoặc biến một đề xuất thành cải thiện đã được chứng minh. "
    "Nếu chưa đủ thông tin để hiểu cơ chế, nói ngắn gọn rằng đoạn trích chưa mô tả nó.\n"
    "- Chỉ liên hệ với embedded/5G khi đầu vào nêu rõ mối liên hệ. Không thêm "
    "ứng dụng giả định, lời khuyên triển khai hay câu quảng bá về tác động với kỹ sư.\n"
    "- Với tin phát hành/phần cứng/công cụ: nêu thay đổi cụ thể và tác dụng được "
    "nguồn mô tả. Với câu hỏi cộng đồng: nêu vấn đề và điều kiện người viết đưa ra, "
    "không tự trả lời câu hỏi.\n\n"
)

# Bước 2: Gemini Flash — tóm tắt (giữ nguyên tiếng Anh) cho phần lớn bài
GEMINI_SUMMARIZE_PROMPT = (
    "Bạn là trợ lý tổng hợp tin tức công nghệ cho kỹ sư embedded/firmware mảng 5G RAN.\n\n"
    "Với danh sách bài viết JSON (mỗi bài có title/summary), với MỖI bài trả về:\n"
    "- title: giữ nguyên tiêu đề gốc\n"
    "- summary_vi: tóm tắt 1-2 câu BẰNG TIẾNG ANH (không dịch sang tiếng Việt), "
    "tối đa 70 từ, ưu tiên thông tin cụ thể giúp quyết định có mở bài đọc hay không.\n\n"
    + _SUMMARY_CONTENT_RULES
    + 'Dạng trả về: {"items": [{"title": "...", "summary_vi": "..."}, ...]}\n'
    + _JSON_RULE
)

# Bước 3: Claude Haiku — phân tích sâu (giữ nguyên tiếng Anh) cho bài kỹ thuật cao (relevance = 5)
CLAUDE_DEEP_PROMPT = (
    "Bạn là chuyên gia phân tích kỹ thuật cho kỹ sư embedded/firmware mảng 5G RAN.\n\n"
    "Tóm tắt rõ nội dung kỹ thuật của các bài sau từ title/link/summary được cung cấp.\n"
    "Với MỖI bài, hãy trả về:\n"
    "- title: giữ nguyên tiêu đề gốc\n"
    "- summary_vi: tóm tắt 2-3 câu BẰNG TIẾNG ANH (không dịch sang tiếng Việt), "
    "tối đa 90 từ; dành chỗ cho cơ chế, kết quả và điều kiện áp dụng có căn cứ, "
    "không kéo dài nếu đoạn trích chỉ cung cấp ít thông tin.\n\n"
    + _SUMMARY_CONTENT_RULES
    + 'Dạng trả về: {"items": [{"title": "...", "summary_vi": "..."}, ...]}\n'
    + _JSON_RULE
)


def _parse_json_safe(raw_text: str, source: str = "AI") -> dict | None:
    """Parse JSON từ model, kể cả khi response bị bọc trong code fence/text.

    Trả về dict khi parse được; nếu thất bại hoàn toàn, in preview kèm tên
    `source` để chẩn đoán rồi trả về None.
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
    print(f"[cảnh báo] {source} không trả JSON hợp lệ, dùng tóm tắt gốc thay thế")
    print(f"[debug]    raw_text preview: {preview!r}")
    return None


def _gemini_call(client: genai.Client, model_name: str, prompt: str, payload: list[dict]) -> dict | None:
    """Gọi một Gemini model với prompt/payload rồi parse response thành dict.

    Lỗi API hoặc response không có text được log và chuyển thành None để các
    pipeline step phía trên áp dụng fallback thay vì propagate exception.
    """
    try:
        response = client.models.generate_content(
            model=model_name,
            contents=prompt + "\n\n" + json.dumps(payload, ensure_ascii=False),
            config=types.GenerateContentConfig(),
        )
    except Exception as exc:
        # In loại lỗi + repr đầy đủ (str(exc) đôi khi rút gọn, thiếu status
        # code/lý do thật của lỗi API), cộng traceback để debug sâu nếu cần.
        print(f"[Gemini:{model_name}] Lỗi API — loại: {type(exc).__name__}")
        print(f"[Gemini:{model_name}] Chi tiết: {exc!r}")
        traceback.print_exc()
        return None

    # Gọi API thành công nhưng response có thể rỗng (bị safety filter chặn,
    # hoặc model không sinh ra text) — SDK mới có thể trả response.text = None.
    # Giữ nhánh diagnostic/fallback cho trường hợp không có text.
    try:
        raw_text = response.text
        if raw_text is None:
            raise ValueError("response.text is None")
        raw_text = raw_text.strip()
    except ValueError as exc:
        print(f"[Gemini:{model_name}] Response không có text hợp lệ: {exc}")
        try:
            print(f"[Gemini:{model_name}] finish_reason: {response.candidates[0].finish_reason}")
            print(f"[Gemini:{model_name}] safety_ratings: {response.candidates[0].safety_ratings}")
        except Exception:
            print(f"[Gemini:{model_name}] Không đọc được thêm chi tiết candidates")
        return None

    return _parse_json_safe(raw_text, source=f"Gemini:{model_name}")


def _step1_filter(entries: list[dict], client: genai.Client) -> tuple[list[dict], list[dict]]:
    """Bước 1 — Gemini Flash-Lite: chấm relevance.

    Trả về `(passed, dropped)`; mỗi entry là bản sao có thêm relevance. Bài
    `dropped` vẫn cần đi tới update_state() để được đánh dấu `rejected`, tránh
    bị fetch và chấm điểm lại ở lần chạy sau.

    Fail-open: nếu Gemini lỗi → coi tất cả đạt ngưỡng (an toàn hơn loại
    nhầm khi không chắc) và không trả bài nào trong `dropped`.
    """
    payload = [
        {"title": e["title"], "summary": e["summary"][:300]}
        for e in entries
    ]
    parsed = _gemini_call(client, GEMINI_FILTER_MODEL, GEMINI_FILTER_PROMPT, payload)

    if parsed is None:
        print("[Bước 1] Gemini Filter lỗi — giữ toàn bộ bài, gán relevance mặc định")
        return [{**e, "relevance": MIN_RELEVANCE_PREFILTER} for e in entries], []

    by_title = {item.get("title"): item for item in parsed.get("items", [])}
    scored = []
    for e in entries:
        relevance = by_title.get(e["title"], {}).get("relevance", MIN_RELEVANCE_PREFILTER)
        scored.append({**e, "relevance": relevance})

    passed = [e for e in scored if e["relevance"] >= MIN_RELEVANCE_PREFILTER]
    dropped = [e for e in scored if e["relevance"] < MIN_RELEVANCE_PREFILTER]
    print(f"[Bước 1] {len(entries)} bài → lọc bỏ {len(dropped)} → còn {len(passed)} bài")
    return passed, dropped


def _step2_summarize(entries: list[dict], client: genai.Client) -> list[dict]:
    """Bước 2 — Gemini Flash: tạo summary tiếng Anh cho các bài đã qua lọc.

    Trả bản sao entry có thêm `summary_vi` (tên field hiện tại, nội dung tiếng
    Anh). Fail-open: nếu Gemini lỗi hoặc thiếu item, dùng 200 ký tự đầu của
    summary RSS gốc.
    """
    payload = [
        {"title": e["title"], "summary": e["summary"][:500]}
        for e in entries
    ]
    parsed = _gemini_call(client, GEMINI_SUMMARIZE_MODEL, GEMINI_SUMMARIZE_PROMPT, payload)

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
    """Bước 3 — dùng Claude ghi đè summary của bài đạt ngưỡng kỹ thuật sâu.

    Các bài được gọi theo batch; response bị cắt hoặc không parse được sẽ giữ
    summary Gemini. Lỗi API Claude không bị nuốt và sẽ propagate cho caller.
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

        parsed = _parse_json_safe(response.content[0].text.strip(), source="Claude")
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
    """Chạy pipeline AI cho danh sách entry mới do fetch.py trả về.

    Hàm gọi Gemini và có thể gọi Claude, rồi trả cả bài đạt ngưỡng đã có
    summary lẫn bài bị loại ở Bước 1. Bài bị loại chỉ có relevance để main.py
    ghi state `rejected`; send_telegram.py sẽ không hiển thị chúng.
    """
    if not entries:
        return []

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    # Bước 1: Gemini Flash-Lite — lọc relevance (rẻ nhất)
    passed, dropped = _step1_filter(entries, client)
    if not passed:
        print("[pipeline] Không có bài nào qua lọc.")
        return dropped

    # Bước 2: Gemini Flash — tóm tắt tiếng Anh (phần lớn công việc)
    summarized = _step2_summarize(passed, client)

    # Bước 3: Claude Haiku — chỉ override bài relevance=5 (kỹ thuật sâu)
    result = _step3_claude_deep(summarized)
    return result + dropped
