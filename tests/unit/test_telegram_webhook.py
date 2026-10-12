import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

import asyncpg
from fastapi import BackgroundTasks, FastAPI, Request
from pytest import MonkeyPatch

import harle_api.routes.telegram as app_module
from harle_domain.messaging import (
    OutboundMessenger,
    RecentMediaStore,
    TelegramMediaDownloader,
)
from harle_domain.profiles import UserProfileRepository
from harle_services.access import PreflightService, TemporaryBan
from harle_services.bootstrap import ProcessRuntime, TelegramRuntime
from harle_services.events import AgentsScheduler
from harle_services.messaging import (
    MessageCoordinator,
    MessageFragment,
    MessageSubmission,
    MessageSubmissionStatus,
)
from harle_services.runtime import UserRuntimeFactory
from harle_services.tools import ToolsInjector


@dataclass(frozen=True)
class FakeSettings:
    TELEGRAM_WEBHOOK_SECRET: str = "secret"
    TELEGRAM_BOT_TOKEN: str = "token"
    MAX_MEDIA_REQUEST_SIZE: int = 12 * 1024 * 1024


class FakeMessages:
    def __init__(self, statuses: list[MessageSubmissionStatus]) -> None:
        self.statuses = statuses
        self.received: list[int] = []

    async def receive(
        self,
        message: MessageFragment,
    ) -> MessageSubmission:
        self.received.append(message.update_id)
        return self._submission()

    def _submission(self) -> MessageSubmission:
        status = self.statuses.pop(0)
        temporary_ban = (
            TemporaryBan(
                blocked_until=datetime(2026, 9, 11, 4, tzinfo=timezone.utc),
                notify_user=True,
            )
            if status is MessageSubmissionStatus.RATE_LIMITED
            else None
        )
        return MessageSubmission(status, temporary_ban)

    async def reject(
        self,
        *,
        update_id: int,
        telegram_user_id: int,
        telegram_chat_id: int,
        text: str,
    ) -> MessageSubmission:
        del telegram_user_id, telegram_chat_id, text
        self.received.append(update_id)
        return self._submission()


class FakeMessenger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        del chat_id
        self.messages.append(text)

    async def send_typing_action(self, *, chat_id: int) -> None:
        del chat_id


def fake_runtime(
    messages: FakeMessages,
    messenger: FakeMessenger | None = None,
) -> ProcessRuntime:
    return ProcessRuntime(
        pool=cast(asyncpg.Pool, object()),
        preflight=cast(PreflightService, object()),
        users=cast(UserRuntimeFactory, object()),
        tools=cast(ToolsInjector, object()),
        messages=cast(MessageCoordinator, messages),
        profiles=cast(UserProfileRepository, object()),
        telegram=TelegramRuntime(
            messenger=cast(OutboundMessenger, messenger or FakeMessenger()),
            media_downloader=cast(TelegramMediaDownloader, object()),
            recent_media=cast(RecentMediaStore, object()),
            maximum_media_request_size=12 * 1024 * 1024,
        ),
        scheduler=cast(AgentsScheduler, object()),
    )


def test_webhook_deduplicates_accepted_and_rejected_updates(
    monkeypatch: MonkeyPatch,
) -> None:
    async def verify() -> None:
        messages = FakeMessages(
            [
                MessageSubmissionStatus.STARTED,
                MessageSubmissionStatus.DUPLICATE,
                MessageSubmissionStatus.REJECTED,
                MessageSubmissionStatus.DUPLICATE,
            ],
        )
        messenger = FakeMessenger()
        runtime = fake_runtime(messages, messenger)
        application = FastAPI()
        application.state.runtime = runtime
        request = Request({"type": "http", "app": application})
        process_calls = 0

        async def fake_process(**_: object) -> None:
            nonlocal process_calls
            process_calls += 1

        monkeypatch.setattr(app_module, "get_settings", FakeSettings)
        monkeypatch.setattr(app_module, "process_telegram_messages", fake_process)
        update = {
            "update_id": 100,
            "message": {
                "text": "Hello",
                "chat": {"id": 200},
                "from": {"id": 300},
            },
        }

        first_tasks = BackgroundTasks()
        first = await app_module.post_telegram_webhook(
            update=update,
            background_tasks=first_tasks,
            request=request,
            x_telegram_bot_api_secret_token="secret",
        )
        await first_tasks()

        duplicate_tasks = BackgroundTasks()
        duplicate = await app_module.post_telegram_webhook(
            update=update,
            background_tasks=duplicate_tasks,
            request=request,
            x_telegram_bot_api_secret_token="secret",
        )
        await duplicate_tasks()

        rejected_update = {
            "update_id": 101,
            "message": {
                "video": {
                    "file_id": "video",
                    "file_unique_id": "video-unique",
                    "mime_type": "video/mp4",
                },
                "chat": {"id": 200},
                "from": {"id": 300},
            },
        }
        rejected_tasks = BackgroundTasks()
        rejected = await app_module.post_telegram_webhook(
            update=rejected_update,
            background_tasks=rejected_tasks,
            request=request,
            x_telegram_bot_api_secret_token="secret",
        )
        await rejected_tasks()
        rejected_duplicate_tasks = BackgroundTasks()
        rejected_duplicate = await app_module.post_telegram_webhook(
            update=rejected_update,
            background_tasks=rejected_duplicate_tasks,
            request=request,
            x_telegram_bot_api_secret_token="secret",
        )
        await rejected_duplicate_tasks()

        assert first.body == b'{"ok":true,"accepted":true,"disposition":"started"}'
        assert duplicate.body == b'{"ok":true,"accepted":false,"duplicate":true}'
        assert b'"reason":"unsupported_media_type"' in rejected.body
        assert b'"duplicate":true' in rejected_duplicate.body
        assert messages.received == [100, 100, 101, 101]
        assert messenger.messages == ["Formato no soportado."]
        assert process_calls == 1

    asyncio.run(verify())


def test_joined_queued_and_interrupted_updates_do_not_start_another_process(
    monkeypatch: MonkeyPatch,
) -> None:
    async def verify() -> None:
        messages = FakeMessages(
            [
                MessageSubmissionStatus.JOINED,
                MessageSubmissionStatus.QUEUED,
                MessageSubmissionStatus.INTERRUPTED,
            ],
        )
        runtime = fake_runtime(messages)
        application = FastAPI()
        application.state.runtime = runtime
        request = Request({"type": "http", "app": application})
        process_calls = 0

        async def fake_process(**_: object) -> None:
            nonlocal process_calls
            process_calls += 1

        monkeypatch.setattr(app_module, "get_settings", FakeSettings)
        monkeypatch.setattr(app_module, "process_telegram_messages", fake_process)

        responses = []
        for update_id in (100, 101, 102):
            tasks = BackgroundTasks()
            response = await app_module.post_telegram_webhook(
                update={
                    "update_id": update_id,
                    "message": {
                        "text": "Hello",
                        "chat": {"id": 200},
                        "from": {"id": 300},
                    },
                },
                background_tasks=tasks,
                request=request,
                x_telegram_bot_api_secret_token="secret",
            )
            await tasks()
            responses.append(response)

        assert b'"disposition":"joined"' in responses[0].body
        assert b'"disposition":"queued"' in responses[1].body
        assert b'"reason":"interrupted_after_tool_execution"' in responses[2].body
        assert process_calls == 0

    asyncio.run(verify())


def test_rate_limited_update_notifies_without_starting_processing(
    monkeypatch: MonkeyPatch,
) -> None:
    async def verify() -> None:
        messages = FakeMessages([MessageSubmissionStatus.RATE_LIMITED])
        messenger = FakeMessenger()
        runtime = fake_runtime(messages, messenger)
        application = FastAPI()
        application.state.runtime = runtime
        request = Request({"type": "http", "app": application})
        process_calls = 0

        async def fake_process(**_: object) -> None:
            nonlocal process_calls
            process_calls += 1

        monkeypatch.setattr(app_module, "get_settings", FakeSettings)
        monkeypatch.setattr(app_module, "process_telegram_messages", fake_process)

        tasks = BackgroundTasks()
        response = await app_module.post_telegram_webhook(
            update={
                "update_id": 100,
                "message": {
                    "text": "Hello",
                    "chat": {"id": 200},
                    "from": {"id": 300},
                },
            },
            background_tasks=tasks,
            request=request,
            x_telegram_bot_api_secret_token="secret",
        )
        await tasks()

        assert b'"reason":"temporarily_banned"' in response.body
        assert b'"notified":true' in response.body
        assert len(messenger.messages) == 1
        assert process_calls == 0

    asyncio.run(verify())
