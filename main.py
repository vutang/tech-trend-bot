"""
Điểm chạy chính: đọc state -> thu thập -> tóm tắt -> gộp bài tồn kho
-> gửi Telegram -> ghi lại state.
Được GitHub Actions gọi hai phiên mỗi ngày, xem .github/workflows/daily-digest.yml.
"""
import argparse
from datetime import datetime, timezone
from uuid import uuid4

from digest_config import SESSION_CONFIGS, get_config
from fetch import fetch_new_entries
from summarize import summarize_entries
from send_telegram import send_digest, MIN_RELEVANCE
from digest_log import log_run, log_expired
from state import (
    load_state,
    known_ids,
    pending_entries,
    update_state,
    save_state,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Send a technology digest session")
    parser.add_argument("--session", choices=tuple(SESSION_CONFIGS), default="morning")
    return parser.parse_args(argv)


def main(argv=None) -> None:
    config = get_config(parse_args(argv).session)  # before fetch or AI
    context = {"run_id": f"{datetime.now(timezone.utc).isoformat()}-{uuid4().hex}",
               "session": config.session, "cap": config.cap,
               "quotas": config.quotas}
    print(f"[run] {context['run_id']} session={config.session} cap={config.cap} "
          f"quotas={dict(config.quotas)}")
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

    try:
        delivered = send_digest(candidates, session=config.session,
                                cap=config.cap, quotas=config.quotas)
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

    # Ghi log observability. Hàm này tự nuốt mọi lỗi nên không thể làm
    # hỏng digest; xem docstring digest_log.py.
    log_run(candidates, delivered, MIN_RELEVANCE, **context)

    update_state(state, candidates, delivered, MIN_RELEVANCE)
    expired = save_state(state)
    if expired:
        log_expired(expired, **context)


if __name__ == "__main__":
    main()
