from asyncio import CancelledError, Task, create_task, gather
from datetime import datetime
from typing import cast

from asyncpg import PostgresError

from harle_domain.messaging import (
    MediaContent,
    OutboundMessenger,
    TelegramMediaDownloader,
)
from harle_services.access import (
    PreflightAccepted,
    QuotaExceeded,
)
from harle_services.assistant import GeneratedResponse, generate_response
from harle_services.bootstrap import ProcessRuntime
from harle_services.messaging import MessageTurn
from harle_services.runtime import UserRuntime
from harle_services.tools import ToolInjectionContext
from harle_utils import (
    PROCESSING_FAILURES,
    InactiveSubscriptionError,
    MediaDownloadError,
    MessageDeliveryError,
    MissingProfileError,
    UnknownIdentityError,
    log,
)


async def process_telegram_messages(
    *,
    telegram_user_id: int,
    runtime: ProcessRuntime,
) -> None:
    while turn := await runtime.messages.current_turn(telegram_user_id):
        has_next = await _process_turn(
            turn=turn,
            runtime=runtime,
        )
        if not has_next:
            return


async def _process_turn(
    *,
    turn: MessageTurn,
    runtime: ProcessRuntime,
) -> bool:
    try:
        admission = await runtime.preflight.check(turn.telegram_user_id)
    except UnknownIdentityError:
        await _send_notice(
            messenger=runtime.messenger,
            chat_id=turn.telegram_chat_id,
            text=(f"Tu ID {turn.telegram_user_id} no está registrado en el sistema"),
        )
        return await runtime.messages.finish_failed(
            telegram_user_id=turn.telegram_user_id,
            retryable=True,
        )
    except InactiveSubscriptionError:
        return await runtime.messages.finish_failed(
            telegram_user_id=turn.telegram_user_id,
            retryable=True,
        )

    if isinstance(admission, QuotaExceeded):
        await runtime.interactions.record_user_message(
            user_id=admission.user_id,
            update_ids=turn.update_ids,
        )
        await _send_notice(
            messenger=runtime.messenger,
            chat_id=turn.telegram_chat_id,
            text=(
                f"You have {admission.remaining} requests remaining in this "
                "subscription period. "
                f"Your allowance resets at {_utc_boundary(admission.resets_at)}."
            ),
        )
        return await runtime.messages.finish_failed(
            telegram_user_id=turn.telegram_user_id,
            retryable=True,
        )

    if not isinstance(admission, PreflightAccepted):
        raise RuntimeError("Unexpected preflight result.")

    try:
        await runtime.interactions.record_user_message(
            user_id=admission.resolved_user.user.id,
            update_ids=turn.update_ids,
        )
        user_runtime = await runtime.users.create_for_resolved_user(
            resolved_user=admission.resolved_user,
            telegram_chat_id=turn.telegram_chat_id,
        )
        return await _run_admitted_turn(
            telegram_user_id=turn.telegram_user_id,
            user_runtime=user_runtime,
            runtime=runtime,
        )
    except (MissingProfileError, OSError, PostgresError, RuntimeError):
        return await runtime.messages.finish_failed(
            telegram_user_id=turn.telegram_user_id,
            retryable=True,
        )
    finally:
        await runtime.preflight.release(admission.quota_reservation)


async def _run_admitted_turn(
    *,
    telegram_user_id: int,
    user_runtime: UserRuntime,
    runtime: ProcessRuntime,
) -> bool:
    try:
        await runtime.messenger.send_typing_action(
            chat_id=user_runtime.telegram_chat_id,
        )
    except MessageDeliveryError:
        pass

    while turn := await runtime.messages.current_turn(telegram_user_id):
        task = create_task(
            _generate_response(
                turn=turn,
                user_runtime=user_runtime,
                runtime=runtime,
            ),
        )
        generic_task = cast(Task[object], task)
        await runtime.messages.bind_reasoning_task(
            telegram_user_id=telegram_user_id,
            generation=turn.generation,
            task=generic_task,
        )
        try:
            generated = await task
        except CancelledError:
            if await runtime.messages.should_restart(
                telegram_user_id=telegram_user_id,
                generation=turn.generation,
            ):
                continue
            raise
        except MediaDownloadError:
            await _send_notice(
                messenger=runtime.messenger,
                chat_id=user_runtime.telegram_chat_id,
                text="I could not load that image or audio file.",
            )
            return await runtime.messages.finish_failed(
                telegram_user_id=telegram_user_id,
                retryable=False,
            )
        except PROCESSING_FAILURES as exc:
            log.warning("Turn generation failed: %s", type(exc).__name__)
            return await runtime.messages.finish_failed(
                telegram_user_id=telegram_user_id,
                retryable=True,
            )
        finally:
            await runtime.messages.reasoning_finished(
                telegram_user_id=telegram_user_id,
                task=generic_task,
            )

        if await runtime.messages.should_restart(
            telegram_user_id=telegram_user_id,
            generation=turn.generation,
        ):
            continue
        if not await runtime.messages.begin_delivery(
            telegram_user_id=telegram_user_id,
            generation=turn.generation,
        ):
            continue
        try:
            await runtime.messenger.send_message(
                chat_id=turn.telegram_chat_id,
                text=generated.result.response_text,
            )
        except MessageDeliveryError:
            return await runtime.messages.finish_failed(
                telegram_user_id=telegram_user_id,
                retryable=False,
            )
        try:
            await generated.harle.save(
                prompt=turn.prompt,
                run_result=generated.result,
                telegram_update_ids=turn.update_ids,
            )
        except (OSError, PostgresError, RuntimeError):
            return await runtime.messages.finish_failed(
                telegram_user_id=telegram_user_id,
                retryable=False,
            )
        try:
            await runtime.interactions.record_agent_message(
                user_id=user_runtime.resolved_user.user.id,
            )
        except (OSError, PostgresError, RuntimeError) as exc:
            log.warning("Could not record assistant contact: %s", type(exc).__name__)
        return await runtime.messages.finish_delivered(
            telegram_user_id=telegram_user_id,
            update_ids=turn.update_ids,
        )

    return False


async def _generate_response(
    *,
    turn: MessageTurn,
    user_runtime: UserRuntime,
    runtime: ProcessRuntime,
) -> GeneratedResponse:
    media = await _download_media(
        turn,
        runtime.media_downloader,
        runtime.maximum_media_request_size,
    )
    user_id = user_runtime.resolved_user.user.id
    for content in media:
        runtime.recent_media.add(
            user_id=user_id,
            reference=content.reference,
        )
    recent = runtime.recent_media.list_recent(user_id=user_id)
    tool_store = runtime.tools.inject(
        ToolInjectionContext(
            resolved_user=user_runtime.resolved_user,
            timezone=user_runtime.user_profile.timezone,
            prompt=turn.prompt,
            recent_media=recent,
        ),
    )
    return await generate_response(
        prompt=turn.prompt,
        user_runtime=user_runtime,
        tool_store=tool_store,
        media=media,
        on_tool_started=lambda: runtime.messages.mark_tool_started(
            telegram_user_id=turn.telegram_user_id,
            generation=turn.generation,
        ),
    )


async def _download_media(
    turn: MessageTurn,
    downloader: TelegramMediaDownloader,
    maximum_request_size: int,
) -> list[MediaContent]:
    media = list(
        await gather(*(downloader.download(reference) for reference in turn.media)),
    )
    if sum(map(_media_size, media)) > maximum_request_size:
        raise MediaDownloadError("Combined Telegram media exceeds the size limit.")
    return media


def _media_size(content: MediaContent) -> int:
    return len(content.data)


def _utc_boundary(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def _send_notice(
    *,
    messenger: OutboundMessenger,
    chat_id: int,
    text: str,
) -> None:
    try:
        await messenger.send_message(
            chat_id=chat_id,
            text=text,
        )
    except MessageDeliveryError:
        pass
