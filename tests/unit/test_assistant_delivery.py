import asyncio
from types import SimpleNamespace
from typing import cast

from pytest import MonkeyPatch

import harle_api.assistant as assistant_module
from harle_agent.agent import Harle
from harle_agent.models import HarleRunResult
from harle_domain.messaging import (
    OutboundMessenger,
    RecentMediaStore,
    TelegramMediaDownloader,
)
from harle_services.assistant import GeneratedResponse
from harle_services.bootstrap import ProcessRuntime
from harle_services.messaging import MessageCoordinator, MessageFragment, MessageTurn
from harle_services.runtime import UserRuntime
from harle_services.tools import ToolsInjector
from harle_utils import MessageDeliveryError, UnknownIdentityError


class FakeHarle:
    def __init__(self) -> None:
        self.saved = False

    async def save(self, **_: object) -> None:
        self.saved = True


class FakeCoordinator:
    def __init__(self, turn: MessageTurn) -> None:
        self.turn = turn
        self.finished_failed = False

    async def current_turn(self, telegram_user_id: int) -> MessageTurn | None:
        del telegram_user_id
        return self.turn

    async def bind_reasoning_task(self, **_: object) -> None:
        return None

    async def reasoning_finished(self, **_: object) -> None:
        return None

    async def should_restart(self, **_: object) -> bool:
        return False

    async def begin_delivery(self, **_: object) -> bool:
        return True

    async def finish_failed(self, **_: object) -> bool:
        self.finished_failed = True
        return False


class FailingMessenger:
    async def send_typing_action(self, *, chat_id: int) -> None:
        del chat_id

    async def send_message(self, *, chat_id: int, text: str) -> None:
        del chat_id, text
        raise MessageDeliveryError


def test_failed_telegram_delivery_does_not_persist_completion(
    monkeypatch: MonkeyPatch,
) -> None:
    async def verify() -> None:
        turn = MessageTurn(
            telegram_user_id=1,
            telegram_chat_id=2,
            messages=(MessageFragment(3, 1, 2, "Hello"),),
            generation=0,
        )
        coordinator = FakeCoordinator(turn)
        harle = FakeHarle()

        async def generate_response(**_: object) -> GeneratedResponse:
            return GeneratedResponse(
                harle=cast(Harle, harle),
                result=HarleRunResult(response_text="Hi"),
            )

        monkeypatch.setattr(assistant_module, "_generate_response", generate_response)

        await assistant_module._run_admitted_turn(
            telegram_user_id=1,
            user_runtime=cast(
                UserRuntime,
                SimpleNamespace(telegram_chat_id=2),
            ),
            runtime=cast(
                ProcessRuntime,
                SimpleNamespace(
                    messages=cast(MessageCoordinator, coordinator),
                    tools=cast(ToolsInjector, object()),
                    messenger=cast(OutboundMessenger, FailingMessenger()),
                    media_downloader=cast(TelegramMediaDownloader, object()),
                    recent_media=cast(RecentMediaStore, object()),
                ),
            ),
        )

        assert not harle.saved
        assert coordinator.finished_failed

    asyncio.run(verify())


class RecordingMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


def test_unknown_telegram_identity_notifies_sender() -> None:
    async def verify() -> None:
        turn = MessageTurn(
            telegram_user_id=99,
            telegram_chat_id=2,
            messages=(MessageFragment(3, 99, 2, "Hello"),),
            generation=0,
        )
        coordinator = FakeCoordinator(turn)
        messenger = RecordingMessenger()

        class UnknownPreflight:
            async def check(self, telegram_user_id: int) -> object:
                del telegram_user_id
                raise UnknownIdentityError

        await assistant_module._process_turn(
            turn=turn,
            runtime=cast(
                ProcessRuntime,
                SimpleNamespace(
                    messages=cast(MessageCoordinator, coordinator),
                    preflight=UnknownPreflight(),
                    messenger=cast(OutboundMessenger, messenger),
                ),
            ),
        )

        assert messenger.sent == [
            (2, "Tu ID 99 no está registrado en el sistema"),
        ]
        assert coordinator.finished_failed

    asyncio.run(verify())
