"""
Điểm chạy chính: đọc state -> thu thập -> tóm tắt -> gộp bài tồn kho
-> gửi Telegram -> ghi lại state.
Được GitHub Actions gọi mỗi ngày, xem .github/workflows/daily-digest.yml.
"""
from fetch import fetch_new_entries
from summarize import summarize_entries
from send_telegram import send_digest, MIN_RELEVANCE
from state import (
    load_state,
    known_ids,
    pending_entries,
    update_state,
    save_state,
)


def main() -> None:
    state = load_state()

    # Bài mới: bỏ qua mọi id bot đã biết (đã gửi / đang tồn kho / đã loại)
    new_entries = fetch_new_entries(known_ids(state))
    print(f"Tìm thấy {len(new_entries)} bài mới")

    # Bài tồn kho: ĐÃ có summary + relevance từ lần chạy trước, nên chỉ
    # cần xếp hạng lại, không tốn thêm token AI.
    carried = pending_entries(state)
    if carried:
        print(f"Đưa lại {len(carried)} bài tồn kho từ các lần chạy trước")

    summarized = summarize_entries(new_entries) if new_entries else []
    candidates = summarized + carried

    if not candidates:
        print("Không có bài nào để gửi.")
        save_state(state)  # vẫn ghi để dọn bản ghi hết hạn
        return

    try:
        delivered = send_digest(candidates)
    except Exception:
        # Gửi fail giữa chừng (vd chunk 2/3 lỗi mạng) — CỐ TÌNH không gọi
        # update_state/save_state. State giữ nguyên như lúc load_state(),
        # nên lần chạy sau sẽ coi các bài này như chưa xử lý và thử lại.
        # Đánh đổi: nếu user đã thực sự nhận được vài chunk trước khi lỗi,
        # lần sau có thể nhận lại đúng các bài đó — nhưng còn tốt hơn nhiều
        # so với việc state ghi sai (nghĩ đã gửi trong khi chưa, hoặc
        # ngược lại) một cách âm thầm không ai biết.
        print("[lỗi] Gửi Telegram thất bại giữa chừng — không cập nhật state, "
              "các bài này sẽ được thử lại ở lần chạy tiếp theo.")
        raise  # vẫn để job báo đỏ trên Actions, không nuốt lỗi âm thầm

    print(f"Đã gửi {len(delivered)}/{len(candidates)} bài.")

    update_state(state, candidates, delivered, MIN_RELEVANCE)
    save_state(state)


if __name__ == "__main__":
    main()