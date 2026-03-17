from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict

from .discovery import DiscoverySession
from .models import HouseConfig
from .sdg.grid import PairingRefusedError, SDGProtocolError


LOGGER = logging.getLogger(__name__)


def _house_to_json(house: HouseConfig) -> str:
    return json.dumps(asdict(house), indent=2, sort_keys=True)


async def _run(client_name: str, initial_otp: str | None) -> int:
    session = DiscoverySession(client_name=client_name)
    print(f"Client name: {client_name}")
    print(f"Public key: {session.public_key_hex}")
    print(f"Private key: {session.private_key_hex}")
    print("Enter fresh OTP codes. The session identity will be reused across attempts.")

    otp = initial_otp
    try:
        while True:
            if not otp:
                try:
                    otp = input("OTP: ").strip()
                except EOFError:
                    return 1
            if not otp:
                continue

            try:
                house = await session.pair_and_discover(otp)
            except PairingRefusedError as exc:
                print(f"PAIRING_REFUSED {exc}")
            except SDGProtocolError as exc:
                print(f"DISCOVERY_FAILED {exc}")
            except Exception as exc:  # pragma: no cover - live diagnostic helper
                LOGGER.exception("Unexpected discovery failure")
                print(f"UNEXPECTED_ERROR {type(exc).__name__}: {exc}")
            else:
                print("DISCOVERY_SUCCESS")
                print(_house_to_json(house))
                return 0

            otp = None
    finally:
        await session.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry DEVI Smart discovery with one persistent identity.")
    parser.add_argument("--client-name", default="ha-user", help="Client name / phoneName to present to DEVI")
    parser.add_argument("--otp", help="Optional first OTP to try immediately")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    return asyncio.run(_run(args.client_name, args.otp))


if __name__ == "__main__":
    raise SystemExit(main())
