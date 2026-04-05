"""
Tests for store.py — uses tmp_path fixture, no ~/imsg-data/ access.
"""
import pytest

# TODO (Phase 1): Add tests as store.py is implemented.
#
# Suggested tests:
#   test_read_cursor_defaults_to_zero
#   test_write_and_read_cursor
#   test_write_inbox_creates_file
#   test_inbox_exists_deduplication
#   test_write_inbox_atomic (file is complete or absent, never partial)
#   test_append_chat_history_rolling_window
#   test_write_and_read_draft
#   test_move_draft_to_outbox
#   test_move_outbox_to_sent
#   test_move_outbox_to_errors
#   test_frontmatter_roundtrip
