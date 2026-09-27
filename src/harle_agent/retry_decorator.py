from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import wraps
from time import time
from typing import Any

from harle_domain.tools.models import ToolCall, ToolCallResult
from harle_utils import ASSISTANT_FAILURES, log

from .models import HarleResponse
from .settings import get_agent_settings
from .tools import show_tool_results


def retry(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        settings = get_agent_settings()
        attempts = 0
        max_attempts = 1 if func.__name__ == "_call_tool" else settings.MAX_RETRIES
        last_error_message = ""
        start_time = time()
        while attempts < max_attempts:
            attempts += 1
            try:
                result = await func(*args, **kwargs)
                log.info(
                    f"{func.__name__} SUCCEDED in {time() - start_time} seconds with {attempts} attempts",
                )
                return result
            except ASSISTANT_FAILURES as e:
                last_error_message = str(e)
                log.error(f"Attempt {attempts} for {func.__name__} failed: {e}")
        log.warning(
            f"{func.__name__} FAILED in {time() - start_time} seconds with {attempts} attempts",
        )
        if func.__name__ == "_call_tool":
            call = _tool_call_from_arguments(args, kwargs)
            return ToolCallResult(
                called_tool_name=call.tool_name,
                result={"error": f"{last_error_message}. Don't retry."},
            )
        if func.__name__ == "_call_gemini":
            tool_results = kwargs.get("tool_results")
            if tool_results:
                return HarleResponse(
                    action="respond",
                    response=(
                        "I can't respond right now, but these are the results of "
                        f"the tool calls: {show_tool_results(tool_results)}"
                    ),
                )
            return HarleResponse(
                action="respond",
                response="I can't respond right now, sorry !",
            )
        raise RuntimeError(
            f"Unknown error: {func.__name__} failed after {settings.MAX_RETRIES} attempts.",
        )

    return wrapper


def _tool_call_from_arguments(
    args: Sequence[object],
    kwargs: Mapping[str, object],
) -> ToolCall:
    call = kwargs.get("call")
    if call is None and len(args) > 1:
        call = args[1]
    if not isinstance(call, ToolCall):
        raise TypeError("Could not find the failed ToolCall argument.")
    return call
