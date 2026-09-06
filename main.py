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

    delivered = send_digest(candidates)
    print(f"Đã gửi {len(delivered)}/{len(candidates)} bài.")

    update_state(state, candidates, delivered, MIN_RELEVANCE)
    save_state(state)


if __name__ == "__main__":
    main()
