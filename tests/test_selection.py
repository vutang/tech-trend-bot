import copy
import os
import unittest
from collections import Counter
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from digest_config import get_config, validate_config
from send_telegram import (
    _rank_score, _select_top_entries, build_digest, chunk_digest,
    select_entries, send_digest,
)


def entry(i, category="embedded", relevance=5, **extra):
    return dict(id=str(i), link=f"https://example.test/{i}", title=f"Title {i}",
                summary_vi=f"Summary {i}", category=category,
                relevance=relevance, **extra)


def pool():
    return [entry(f"{c}-{i}", c, 3 if c != "embedded" else 5)
            for c in ("ran", "research", "virt", "embedded") for i in range(12)]


class SelectionTests(unittest.TestCase):
    def setUp(self):
        clock = patch("send_telegram.datetime")
        clock.start().now.return_value = datetime(2026, 9, 14, tzinfo=timezone.utc)
        self.addCleanup(clock.stop)

    def test_session_caps_and_quotas(self):
        for session, cap, minimum in (("morning", 10, 2), ("afternoon", 5, 1)):
            with self.subTest(session=session):
                selected = select_entries(pool(), session=session)
                self.assertEqual(len(selected), cap)
                counts = Counter(e["category"] for e in selected)
                for category in ("ran", "research", "virt"):
                    self.assertEqual(counts[category], minimum)
                self.assertEqual(counts["embedded"], cap - 3 * minimum)

    def test_missing_categories_release_slots(self):
        entries = [entry(i) for i in range(12)] + [entry("r", "ran", 3)]
        for session, cap in (("morning", 10), ("afternoon", 5)):
            with self.subTest(session=session):
                chosen = select_entries(entries, session=session)
                self.assertEqual(len(chosen), cap)
                self.assertIn("r", [e["id"] for e in chosen])

    def test_fewer_than_cap(self):
        entries = [entry(1, "ran"), entry(2, "research")]
        for session in ("morning", "afternoon"):
            self.assertEqual(select_entries(entries, session=session), entries)

    def test_quota_only_for_relevant_articles(self):
        entries = [entry("low", "ran", 2)] + [entry(i) for i in range(10)]
        chosen = select_entries(entries, session="afternoon")
        self.assertEqual(len(chosen), 5)
        self.assertNotIn("low", [e["id"] for e in chosen])

    def test_duplicate_links_do_not_consume_quota(self):
        first = entry("r1", "ran")
        duplicate = dict(first, id="duplicate")
        entries = [first, duplicate, entry("r2", "ran", 4)] + pool()
        chosen = select_entries(entries)
        self.assertEqual(len(chosen), 10)
        self.assertIn("r2", [e["id"] for e in chosen])
        self.assertEqual(len({e["link"] for e in chosen}), 10)

    def test_duplicates_across_categories_and_global_fill(self):
        first = entry(1, "ran")
        entries = [first, dict(first, id="other", category="research")]
        entries += [entry(2, "research", 4), entry(3, "virt", 4)]
        entries += [entry(i) for i in range(4, 12)]
        chosen = select_entries(entries, session="afternoon")
        self.assertEqual(len(chosen), 5)
        self.assertEqual(len({e["link"] for e in chosen}), 5)
        self.assertIn("2", [e["id"] for e in chosen])

    def test_pending_competes_with_age_penalty(self):
        old = entry("old", relevance=5, carry_days=2)
        fresh = entry("fresh", relevance=5)
        pending = entry("pending", relevance=5, carry_days=1)
        self.assertEqual(_rank_score(old), 4)
        chosen = select_entries([old, fresh, pending], session="afternoon", cap=2, quotas={})
        self.assertEqual([e["id"] for e in chosen], ["fresh", "pending"])
        self.assertEqual(old["relevance"], 5)

    def test_selection_does_not_mutate_inputs(self):
        entries = pool()
        original = copy.deepcopy(entries)
        quotas = {"ran": 1}
        select_entries(entries, session="afternoon", cap=5, quotas=quotas)
        self.assertEqual(entries, original)
        self.assertEqual(quotas, {"ran": 1})

    def test_config_is_immutable_snapshot(self):
        quotas = {"ran": 1}
        config = get_config("afternoon", quotas=quotas)
        quotas["ran"] = 99
        self.assertEqual(config.quotas["ran"], 1)
        with self.assertRaises(TypeError):
            config.quotas["ran"] = 99
        self.assertEqual(get_config("afternoon").quotas["ran"], 1)

    def test_invalid_configs_rejected_even_for_empty_selection(self):
        cases = [
            ("night", 10, {}), ([], 10, {}), (None, 10, {}), ("morning", 0, {}), ("morning", -1, {}),
            ("morning", 10.0, {}), ("morning", True, {}),
            ("morning", 10, {"ran": -1}), ("morning", 10, {"ran": 1.5}),
            ("morning", 10, {"ran": True}), ("morning", 10, []),
            ("morning", 10, {"": 1}), ("morning", 10, {"ran": 11}),
            ("morning", 10, {"ran": 7}), ("afternoon", 5, {"ran": 4}),
            ("afternoon", 5, {"ran": 2, "virt": 2, "research": 2}),
        ]
        for session, cap, quotas in cases:
            for fn in (select_entries, _select_top_entries):
                with self.subTest(session=session, cap=cap, quotas=quotas, fn=fn.__name__):
                    with self.assertRaises(ValueError):
                        fn([], session=session, cap=cap, quotas=quotas)

    def test_zero_quota_is_valid(self):
        validate_config("afternoon", 5, {"ran": 0})

    @patch.dict(os.environ, {}, clear=True)
    @patch("send_telegram.requests.post")
    def test_empty_selection_never_calls_telegram(self, post):
        for entries in ([], [entry(1, relevance=2)]):
            self.assertEqual(send_digest(entries, session="afternoon"), [])
        post.assert_not_called()

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}, clear=True)
    @patch("send_telegram.requests.post")
    def test_preview_matches_sent_selection_and_label(self, post):
        post.return_value = Mock(ok=True)
        for session in ("morning", "afternoon"):
            post.reset_mock()
            config = get_config(session)
            kwargs = dict(session=session, cap=config.cap, quotas=config.quotas)
            entries = pool()
            selected = send_digest(entries, **kwargs)
            sent = "\n".join(c.kwargs["data"]["text"] for c in post.call_args_list)
            self.assertEqual(sent, build_digest(entries, **kwargs))
            self.assertIn(session.title(), sent)
            self.assertEqual(len(selected), config.cap)

    def test_chunking_preserves_article_and_category_blocks(self):
        entries = [entry(i, "ran") for i in range(5)]
        chunks = chunk_digest(entries, limit=130, session="afternoon")
        self.assertGreater(len(chunks), 1)
        for e in entries:
            self.assertTrue(any(f"• {e['title']} 🔥\n  {e['summary_vi']}\n  🔗 {e['link']}" in c
                                for c in chunks))
        self.assertTrue(any("== 5G/5G-A/6G RAN ==\n• Title 0" in c for c in chunks))

    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "test"}, clear=True)
    @patch("send_telegram.requests.post")
    def test_partial_send_failure_propagates(self, post):
        response = Mock(ok=False, status_code=500, text="failure")
        response.raise_for_status.side_effect = RuntimeError("second chunk failed")
        post.side_effect = [Mock(ok=True), response]
        entries = [dict(entry(i), summary_vi="x" * 1800) for i in range(5)]
        with self.assertRaises(RuntimeError):
            send_digest(entries, session="afternoon")
        self.assertEqual(post.call_count, 2)
