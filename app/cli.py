"""Admin CLI: ``python -m app.cli create-api-key --name NAME`` / ``revoke-api-key --prefix P``.

Key management is deliberately not an HTTP endpoint: issuing keys needs a more privileged
credential than the keys themselves, and shell access to the deployment is that credential.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.services.auth import ApiKeyService


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as session:
            service = ApiKeyService(session, settings.api_key_pepper.get_secret_value().encode())
            if args.command == "create-api-key":
                raw_key, api_key = await service.issue(args.name)
                if args.quiet:
                    print(raw_key)
                else:
                    print(f"Created API key '{api_key.name}' (prefix {api_key.prefix}).")
                    print("Store it now; it cannot be shown again:")
                    print(raw_key)
                return 0
            revoked = await service.revoke(args.prefix)
            print(f"Revoked {revoked} key(s) with prefix {args.prefix}.")
            return 0 if revoked else 1
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None, settings: Settings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-api-key", help="Issue a new API key")
    create.add_argument("--name", required=True, help="Who or what the key is for")
    create.add_argument("--quiet", action="store_true", help="Print only the key")
    revoke = commands.add_parser("revoke-api-key", help="Revoke keys by display prefix")
    revoke.add_argument("--prefix", required=True, help="The key's first 12 characters")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args, settings or get_settings()))


if __name__ == "__main__":
    sys.exit(main())
