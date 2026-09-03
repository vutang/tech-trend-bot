"""
Điểm chạy chính: thu thập -> tóm tắt -> gửi Telegram.
Được GitHub Actions gọi mỗi ngày, xem .github/workflows/daily-digest.yml.
"""
from fetch import fetch_new_entries
from summarize import summarize_entries
from send_telegram import send_digest


def main() -> None:
    entries = fetch_new_entries()
    print(f"Tìm thấy {len(entries)} bài mới")

    if not entries:
        print("Không có bài mới, bỏ qua bước gửi tin.")
        return

    summarized = summarize_entries(entries)
    send_digest(summarized)
    print("Đã gửi digest thành công.")


if __name__ == "__main__":
    main()
