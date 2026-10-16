"""Unit tests for bot.history — SQLite chat history."""

import time

from bot import history


class TestAddAndGetHistory:
    def test_add_message_then_get(self):
        history.add_message(1, "user", "hola")
        rows = history.get_history(1, 20)
        assert rows == [{"role": "user", "content": "hola"}]

    def test_history_isolated_per_user(self):
        history.add_message(1, "user", "user one")
        history.add_message(2, "user", "user two")
        assert history.get_history(2, 20) == [{"role": "user", "content": "user two"}]

    def test_get_history_respects_limit(self):
        for i in range(10):
            history.add_message(1, "user", f"msg {i}")
        rows = history.get_history(1, 2)
        # limit is multiplied by 2 (user + assistant per exchange)
        assert len(rows) == 4
        # the window is the MOST RECENT messages, still oldest-first inside
        assert rows[0]["content"] == "msg 6"
        assert rows[-1]["content"] == "msg 9"

    def test_get_history_returns_most_recent_window(self):
        # Regression test: get_history must return the LAST N messages, not
        # the first N. If someone reverts the query to plain
        # ORDER BY created_at ASC LIMIT ?, this fails.
        # Explicit created_at makes the order deterministic regardless of
        # how fast the loop runs (time.time() can collide on fast machines).
        with history._get_connection() as conn:
            for i in range(10):
                conn.execute(
                    "INSERT INTO chat_history (user_id, chat_id, role, content, created_at)"
                    " VALUES (1, 1, 'user', ?, ?)",
                    (f"msg {i}", 1000 + i),
                )
            conn.commit()

        rows = history.get_history(1, 2)  # limit 2 exchanges -> 4 rows

        assert [r["content"] for r in rows] == ["msg 6", "msg 7", "msg 8", "msg 9"]
        assert rows[0]["content"] != "msg 0"  # never the beginning of the chat

    def test_get_history_empty(self):
        assert history.get_history(99, 20) == []


class TestClearHistory:
    def test_clears_only_target_user(self):
        history.add_message(1, "user", "keep me")
        history.add_message(2, "user", "delete me")
        history.clear_history(2)
        assert history.get_history(1, 20) == [{"role": "user", "content": "keep me"}]
        assert history.get_history(2, 20) == []


class TestChatScopedHistory:
    """Tests for chat_id-scoped history isolation."""

    def test_add_message_with_chat_id(self):
        history.add_message(1, "user", "hello group", chat_id=-1001)
        rows = history.get_history(1, 20, chat_id=-1001)
        assert rows == [{"role": "user", "content": "hello group"}]

    def test_history_isolated_per_chat(self):
        # Same user in different chats should see different histories
        history.add_message(1, "user", "private msg", chat_id=1)
        history.add_message(1, "user", "group msg", chat_id=-1001)

        private_history = history.get_history(1, 20, chat_id=1)
        group_history = history.get_history(1, 20, chat_id=-1001)

        assert private_history == [{"role": "user", "content": "private msg"}]
        assert group_history == [{"role": "user", "content": "group msg"}]

    def test_group_and_private_histories_are_independent(self):
        # Simulate the original bug: user talks in group, then private
        # Without chat_id scoping, the bot would mix these up
        history.add_message(1, "user", "Lista los proyectos", chat_id=-1001)
        history.add_message(1, "assistant", "No hay proyectos en este grupo", chat_id=-1001)
        history.add_message(1, "user", "Dame la info del proyecto", chat_id=1)
        history.add_message(1, "assistant", "Tienes el proyecto Cédula Catastral", chat_id=1)

        group_history = history.get_history(1, 20, chat_id=-1001)
        private_history = history.get_history(1, 20, chat_id=1)

        # Group should only see group conversation
        assert len(group_history) == 2
        assert group_history[0]["content"] == "Lista los proyectos"
        assert group_history[1]["content"] == "No hay proyectos en este grupo"

        # Private should only see private conversation
        assert len(private_history) == 2
        assert private_history[0]["content"] == "Dame la info del proyecto"
        assert private_history[1]["content"] == "Tienes el proyecto Cédula Catastral"

    def test_clear_history_scoped_to_chat(self):
        history.add_message(1, "user", "private msg", chat_id=1)
        history.add_message(1, "user", "group msg", chat_id=-1001)

        # Clear only the group chat
        history.clear_history(1, chat_id=-1001)

        # Private chat should be untouched
        assert history.get_history(1, 20, chat_id=1) == [
            {"role": "user", "content": "private msg"}
        ]
        # Group chat should be empty
        assert history.get_history(1, 20, chat_id=-1001) == []

    def test_clear_history_without_chat_id_clears_all(self):
        history.add_message(1, "user", "private msg", chat_id=1)
        history.add_message(1, "user", "group msg", chat_id=-1001)

        # Clear all without chat_id
        history.clear_history(1)

        assert history.get_history(1, 20, chat_id=1) == []
        assert history.get_history(1, 20, chat_id=-1001) == []

    def test_backward_compat_no_chat_id_defaults_to_user_id(self):
        # When chat_id is not provided, it should default to user_id
        # This preserves backward compatibility for existing callers
        history.add_message(1, "user", "old style msg")
        rows = history.get_history(1, 20)
        assert rows == [{"role": "user", "content": "old style msg"}]

    def test_multiple_users_same_group(self):
        # Multiple users in the same group should have isolated histories
        history.add_message(1, "user", "user1 msg", chat_id=-1001)
        history.add_message(2, "user", "user2 msg", chat_id=-1001)

        user1_history = history.get_history(1, 20, chat_id=-1001)
        user2_history = history.get_history(2, 20, chat_id=-1001)

        assert user1_history == [{"role": "user", "content": "user1 msg"}]
        assert user2_history == [{"role": "user", "content": "user2 msg"}]

    def test_get_history_respects_limit_per_chat(self):
        # Add messages to different chats
        for i in range(5):
            history.add_message(1, "user", f"private {i}", chat_id=1)
        for i in range(5):
            history.add_message(1, "user", f"group {i}", chat_id=-1001)

        # Limit should apply per chat
        private_rows = history.get_history(1, 2, chat_id=1)
        group_rows = history.get_history(1, 2, chat_id=-1001)

        # Each should get the last 4 messages (2 * 2)
        assert len(private_rows) == 4
        assert len(group_rows) == 4

        # Verify the content is from the correct chat
        assert all("private" in r["content"] for r in private_rows)
        assert all("group" in r["content"] for r in group_rows)
