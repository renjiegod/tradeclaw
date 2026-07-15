#!/usr/bin/env python3
"""Validate and optionally send representative Feishu Card JSON 2.0 cards.

Default mode is offline validation only:

    uv run python scripts/feishu_card_smoke.py --card all

Real API smoke mode requires a test chat/user target:

    FEISHU_APP_ID=cli_x FEISHU_APP_SECRET=... \
    uv run python scripts/feishu_card_smoke.py \
      --send --receive-id oc_xxx --receive-id-type chat_id --card approval_pending

Use a disposable test group. This script sends real messages when ``--send`` is
present.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from doyoutrade.assistant.channels.feishu.card.cardkit import CardKitClient
from doyoutrade.assistant.channels.feishu.card.validation import (
    sample_feishu_cards,
    validate_card_json_v2,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--card",
        default="all",
        help="Card sample name to validate/send, or 'all'. Use --list to inspect names.",
    )
    parser.add_argument("--list", action="store_true", help="List sample card names and exit.")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Actually send selected cards via Feishu OpenAPI. Omit for offline validation only.",
    )
    parser.add_argument(
        "--via",
        choices=["direct", "cardkit", "both"],
        default="direct",
        help="Send path: direct message.create card JSON, CardKit create+send, or both.",
    )
    parser.add_argument("--receive-id", default=os.getenv("FEISHU_RECEIVE_ID"))
    parser.add_argument(
        "--receive-id-type",
        default=os.getenv("FEISHU_RECEIVE_ID_TYPE", "chat_id"),
        help="Feishu receive_id_type, e.g. chat_id/open_id/user_id.",
    )
    parser.add_argument("--app-id", default=os.getenv("FEISHU_APP_ID"))
    parser.add_argument("--app-secret", default=os.getenv("FEISHU_APP_SECRET"))
    parser.add_argument(
        "--domain",
        choices=["feishu", "lark"],
        default=os.getenv("FEISHU_DOMAIN", "feishu"),
    )
    return parser


def _selected_cards(all_cards: dict[str, dict[str, Any]], name: str) -> dict[str, dict[str, Any]]:
    if name == "all":
        return all_cards
    if name not in all_cards:
        raise SystemExit(f"unknown --card {name!r}; run --list")
    return {name: all_cards[name]}


def _validate(cards: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for name, card in cards.items():
        errors.extend(validate_card_json_v2(card, name=name))
    return errors


def _send_direct(
    client: CardKitClient,
    *,
    name: str,
    card: dict[str, Any],
    receive_id: str,
    receive_id_type: str,
) -> dict[str, Any]:
    message_id = client.send_card_json(
        card=card,
        receive_id=receive_id,
        receive_id_type=receive_id_type,
    )
    return {
        "card": name,
        "path": "direct",
        "ok": bool(message_id),
        "message_id": message_id,
        "error": client.last_error,
    }


def _send_cardkit(
    client: CardKitClient,
    *,
    name: str,
    card: dict[str, Any],
    receive_id: str,
    receive_id_type: str,
) -> dict[str, Any]:
    card_id = client.create_card(card)
    create_error = client.last_error
    message_id = None
    send_error = None
    if card_id:
        message_id = client.send_card_by_card_id(
            card_id=card_id,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
        )
        send_error = client.last_error
    return {
        "card": name,
        "path": "cardkit",
        "ok": bool(card_id and message_id),
        "card_id": card_id,
        "message_id": message_id,
        "create_error": create_error,
        "send_error": send_error,
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    all_cards = sample_feishu_cards()
    if args.list:
        print(json.dumps({"cards": sorted(all_cards)}, ensure_ascii=False, indent=2))
        return 0

    cards = _selected_cards(all_cards, args.card)
    errors = _validate(cards)
    if errors:
        print(
            json.dumps(
                {"ok": False, "phase": "validate", "errors": errors},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    if not args.send:
        print(
            json.dumps(
                {"ok": True, "phase": "validate", "cards": sorted(cards)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    missing = [
        name
        for name, value in (
            ("--app-id/FEISHU_APP_ID", args.app_id),
            ("--app-secret/FEISHU_APP_SECRET", args.app_secret),
            ("--receive-id/FEISHU_RECEIVE_ID", args.receive_id),
        )
        if not value
    ]
    if missing:
        print(
            json.dumps(
                {"ok": False, "phase": "preflight", "missing": missing},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2

    client = CardKitClient(args.app_id, args.app_secret, domain=args.domain)
    results: list[dict[str, Any]] = []
    for name, card in cards.items():
        if args.via in ("direct", "both"):
            results.append(
                _send_direct(
                    client,
                    name=name,
                    card=card,
                    receive_id=args.receive_id,
                    receive_id_type=args.receive_id_type,
                )
            )
        if args.via in ("cardkit", "both"):
            results.append(
                _send_cardkit(
                    client,
                    name=name,
                    card=card,
                    receive_id=args.receive_id,
                    receive_id_type=args.receive_id_type,
                )
            )

    ok = all(item.get("ok") for item in results)
    print(json.dumps({"ok": ok, "phase": "send", "results": results}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
