"""Lightweight fake Telegram objects for integration tests.

The bot's handlers only touch a small surface of the PTB objects:
- Update.effective_user.id, Update.effective_chat.id
- Update.message.text / .reply_text / .photo / .voice
- Update.callback_query.data / .message / .from_user / .answer()
- Context.args, Context.bot_data, Context.bot.send_*

These fakes capture calls so tests can assert what the bot would send
without any network access.
"""


class FakeUser:
    def __init__(
        self,
        user_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ):
        self.id = user_id
        self.username = username
        self.first_name = first_name


class FakeChat:
    def __init__(self, chat_id: int, chat_type: str = "private"):
        self.id = chat_id
        self.type = chat_type


class FakeMessage:
    def __init__(self, text: str | None = None, chat_id: int = 1,
                 user_id: int = 1, photo=None, voice=None, caption: str | None = None):
        self.text = text
        self.caption = caption
        self.photo = photo or []
        self.voice = voice
        self.replies: list[dict] = []
        self.edited_messages: list[dict] = []
        self.edited_reply_markups: list[dict] = []

    async def reply_text(self, text: str, parse_mode=None, **kwargs):
        self.replies.append({"text": text, "parse_mode": parse_mode, **kwargs})
        return text

    async def edit_message_text(self, text: str, parse_mode=None, **kwargs):
        self.edited_messages.append({"text": text, "parse_mode": parse_mode, **kwargs})
        return True

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        self.edited_reply_markups.append({"reply_markup": reply_markup, **kwargs})
        return True


class FakeCallbackQuery:
    """Fake of PTB CallbackQuery for inline button tests.

    Carries the callback_data plus the message/user the button belongs to,
    and records every answer() call so tests can assert the bot's reply.
    """

    def __init__(self, data: str, message: FakeMessage | None = None,
                 user_id: int = 1, chat_id: int = 1):
        self.data = data
        self.message = message
        self.from_user = FakeUser(user_id)
        self.chat_id = chat_id
        self.answered = False
        self.answers: list[dict] = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answered = True
        self.answer_text = text
        self.show_alert = show_alert
        self.answers.append({"text": text, "show_alert": show_alert, **kwargs})
        return True

    async def edit_message_text(self, text: str, parse_mode=None, **kwargs):
        """Mirror PTB's CallbackQuery.edit_message_text (PTB v22 API).

        Delegates to the associated message so tests can keep asserting on
        ``query.message.edited_messages``; a query without a message is a
        silent no-op, matching how the production code guards with
        ``if query.message:``.
        """
        if self.message is None:
            return None
        return await self.message.edit_message_text(text, parse_mode=parse_mode, **kwargs)


class FakeUpdate:
    def __init__(self, text: str | None = None, user_id: int = 1,
                 chat_id: int = 1, photo=None, voice=None, caption: str | None = None,
                 callback_query: FakeCallbackQuery | None = None, chat_type: str = "private"):
        self.callback_query = callback_query
        if callback_query is not None:
            # Mirrors PTB: a callback-query update exposes the query's own
            # message and user as the effective ones.
            self.message = callback_query.message
            self.effective_user = callback_query.from_user
            self.effective_chat = FakeChat(callback_query.chat_id)
        else:
            self.message = FakeMessage(
                text=text, chat_id=chat_id, user_id=user_id,
                photo=photo, voice=voice, caption=caption,
            )
            self.effective_user = FakeUser(user_id)
            self.effective_chat = FakeChat(chat_id, chat_type)


class FakeBot:
    def __init__(self):
        self.sent_messages: list[dict] = []
        self.sent_photos: list[dict] = []
        self.sent_actions: list[tuple] = []

    async def send_chat_action(self, chat_id: int, action: str, **kwargs):
        self.sent_actions.append((chat_id, action))

    async def send_message(self, chat_id: int, text: str, parse_mode=None, **kwargs):
        self.sent_messages.append({"chat_id": chat_id, "text": text,
                                   "parse_mode": parse_mode, **kwargs})

    async def send_photo(self, chat_id: int, photo, caption=None,
                         parse_mode=None, **kwargs):
        self.sent_photos.append({"chat_id": chat_id, "photo": photo,
                                 "caption": caption, "parse_mode": parse_mode, **kwargs})


class FakeContext:
    """Minimal context exposing the attributes handlers use."""

    def __init__(self, settings, ai_client=None, args=None):
        self.bot = FakeBot()
        self.bot_data = {
            "settings": settings,
            "provider": settings.ai_provider,
            "ai_client": ai_client,
        }
        self.args = list(args or [])
