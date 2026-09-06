"""
Quản lý state của bot — thay cho seen.json (list phẳng) trước đây.

Ba nhóm, tách biệt vì mục đích khác nhau:

- delivered: đã gửi cho user. Không bao giờ gửi lại.
- pending:   đã xử lý qua AI (có summary + relevance) nhưng chưa lọt top N.
             Được xét lại ở các lần chạy sau, tối đa PENDING_TTL_DAYS ngày.
             Lưu nguyên entry đã xử lý nên khi xét lại KHÔNG tốn thêm token.
- rejected:  điểm quá thấp hoặc hết hạn pending. Không xét lại, chỉ giữ id
             để không fetch/xử lý lại từ đầu.

Bản cũ gộp cả ba vào một list "seen" đánh dấu ngay lúc fetch, nên một bài
tốt bị mất vĩnh viễn chỉ vì hôm đó tình cờ có nhiều bài tốt hơn.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path(__file__).parent / "state.json"
LEGACY_SEEN_FILE = Path(__file__).parent / "seen.json"

PENDING_TTL_DAYS = 3        # bài chưa gửi được giữ lại tối đa bao lâu
HISTORY_TTL_DAYS = 30       # giữ id đã gửi/đã loại bao lâu để chống trùng
AGE_PENALTY_PER_DAY = 0.5   # trừ điểm relevance mỗi ngày nằm trong pending


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str) -> datetime:
    """Parse ISO timestamp, fallback về hiện tại nếu hỏng (không để crash)."""
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return _now()
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def known_ids(state: dict) -> set:
    """Tất cả id bot đã biết — dùng để fetch.py bỏ qua, không xử lý lại."""
    return set(state["delivered"]) | set(state["pending"]) | set(state["rejected"])


def load_state() -> dict:
    """Đọc state.json (nếu có), rồi UNION thêm mọi id từ seen.json cũ mà
    chưa xuất hiện ở đâu trong state hiện tại.

    Idempotent — gọi bao nhiêu lần cũng an toàn: sau lần đầu, seen.json
    không còn id nào mới để union nữa nên các lần sau chỉ là no-op. Khác
    với bản trước chỉ migrate khi CHƯA có state.json — cách đó làm mất dữ
    liệu nếu state.json được tạo ra (ví dụ lúc test) trước khi seen.json
    thật được đưa vào.
    """
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = {
            "delivered": data.get("delivered", {}),
            "pending": data.get("pending", {}),
            "rejected": data.get("rejected", {}),
        }
    else:
        state = {"delivered": {}, "pending": {}, "rejected": {}}

    if LEGACY_SEEN_FILE.exists():
        with open(LEGACY_SEEN_FILE, "r", encoding="utf-8") as f:
            old_ids = json.load(f)
        already_known = known_ids(state)
        new_from_legacy = [i for i in old_ids if i not in already_known]
        if new_from_legacy:
            ts = _now().isoformat()
            for i in new_from_legacy:
                state["delivered"][i] = ts
            print(f"[state] Union thêm {len(new_from_legacy)} id từ seen.json -> delivered")

    return state


def pending_entries(state: dict) -> list[dict]:
    """Lấy các bài tồn kho, gắn `carry_days` để xếp hạng có trừ điểm theo tuổi.

    Bài quá hạn PENDING_TTL_DAYS không trả về (sẽ bị dọn ở save_state).
    """
    now = _now()
    out = []
    for entry_id, rec in state["pending"].items():
        age = (now - _parse_ts(rec.get("first_seen", ""))).days
        if age >= PENDING_TTL_DAYS:
            continue
        entry = dict(rec.get("entry", {}))
        if not entry.get("id"):
            continue
        entry["carry_days"] = age
        out.append(entry)
    return out


def update_state(state: dict, candidates: list[dict], delivered: list[dict],
                 min_relevance: int) -> None:
    """Cập nhật state sau khi đã gửi digest.

    - Bài được gửi   -> delivered
    - Bài điểm thấp  -> rejected (không đáng xét lại)
    - Còn lại        -> pending, GIỮ NGUYÊN first_seen cũ nếu đã có
                        (nếu ghi đè, bài sẽ tồn kho vĩnh viễn)
    """
    ts = _now().isoformat()
    delivered_ids = {e["id"] for e in delivered}

    for e in delivered:
        state["delivered"][e["id"]] = ts
        state["pending"].pop(e["id"], None)

    for e in candidates:
        entry_id = e.get("id")
        if not entry_id or entry_id in delivered_ids:
            continue

        if e.get("relevance", 3) < min_relevance:
            state["rejected"][entry_id] = ts
            state["pending"].pop(entry_id, None)
            continue

        if entry_id in state["pending"]:
            continue  # đã có, giữ nguyên first_seen để TTL đếm đúng

        clean = {k: v for k, v in e.items() if k != "carry_days"}
        state["pending"][entry_id] = {"first_seen": ts, "entry": clean}


def save_state(state: dict) -> list[dict]:
    """Dọn các bản ghi hết hạn rồi ghi file.

    Trả về danh sách entry vừa hết hạn (đầy đủ dict, không chỉ id) để
    main.py ghi log riêng với status="expired" — KHÔNG được gộp vào
    "rejected" trong log, vì đó là hai lý do khác nhau (nội dung kém vs
    hết hạn vì thời gian). Gộp chung sẽ làm sai chỉ số noise trong
    analyze.py.
    """
    now = _now()
    pending_cutoff = now - timedelta(days=PENDING_TTL_DAYS)
    history_cutoff = now - timedelta(days=HISTORY_TTL_DAYS)

    # Pending hết hạn -> chuyển sang rejected trong STATE (chỉ để không
    # fetch lại — known_ids() không cần phân biệt lý do). Riêng phần data
    # đầy đủ được giữ lại để trả về cho việc ghi log.
    expired_ids = [
        i for i, rec in state["pending"].items()
        if _parse_ts(rec.get("first_seen", "")) < pending_cutoff
    ]
    expired_entries = []
    for i in expired_ids:
        rec = state["pending"].pop(i)
        state["rejected"][i] = rec.get("first_seen", now.isoformat())
        entry = dict(rec.get("entry", {}))
        entry.setdefault("id", i)
        expired_entries.append(entry)
    if expired_ids:
        print(f"[state] {len(expired_ids)} bài hết hạn pending -> rejected")

    # Lịch sử quá cũ -> xoá hẳn để file không phình vô hạn
    for bucket in ("delivered", "rejected"):
        old = [i for i, ts in state[bucket].items() if _parse_ts(ts) < history_cutoff]
        for i in old:
            del state[bucket][i]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)

    print(
        f"[state] delivered={len(state['delivered'])} "
        f"pending={len(state['pending'])} rejected={len(state['rejected'])}"
    )
    return expired_entries