import asyncio
from argparse import ArgumentParser, Namespace
from sys import stderr
from uuid import UUID

from harle_agent.agent import Harle
from harle_agent.models import (
    HarlePersonalContext,
    HarleStores,
    default_harle_config,
)
from harle_agent.stores import FileConversationStore
from harle_domain.tools.models import HarleToolStore
from harle_services.bootstrap import create_tools_injector
from harle_utils import ASSISTANT_FAILURES

CLI_PERSONAL_CONTEXT = HarlePersonalContext(
    user_name="CLI user",
    preferred_name="User",
    locale="und",
    timezone="UTC",
    assistant_profile="Harle is a concise and transparent AI personal assistant.",
    personal_history="No personal history has been supplied.",
)


async def call_harle(harle: Harle, prompt: str) -> None:
    result = await harle.call(prompt)
    if result.response_text:
        print(f"\nGemini: {result.response_text}\n")
    await harle.save(prompt=prompt, run_result=result)


def main() -> int:
    args = _parse_args()

    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("Prompt cannot be empty.", file=stderr)
        return 2

    try:
        tool_store = (
            create_tools_injector().inject_for_explicit_user_id(args.user_id)
            if isinstance(args.user_id, UUID)
            else HarleToolStore()
        )
        harle = Harle(
            config=default_harle_config(),
            stores=HarleStores(
                conversation_store=FileConversationStore(),
                tool_store=tool_store,
            ),
            personal_context=CLI_PERSONAL_CONTEXT,
        )
        asyncio.run(call_harle(harle, prompt))
    except ASSISTANT_FAILURES as exc:
        print(f"Gemini request failed: {exc}", file=stderr)
        return 1

    return 0


def _parse_args() -> Namespace:
    parser = ArgumentParser(
        prog="gemini",
        description="Ask Gemini with Google Search grounding enabled.",
    )
    parser.add_argument(
        "prompt",
        nargs="+",
        help="Prompt to send to Gemini. Quote it to pass it as one argument.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )
    parser.add_argument(
        "--user-id",
        type=UUID,
        help="Explicit internal user UUID used for user-scoped development tools.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
