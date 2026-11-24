"""Command-line authentication, configuration inspection, and one-shot copies."""

import argparse
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from dropbox.exceptions import DropboxException
from requests.exceptions import RequestException

from photo_keeper.auth import create_client, load_credentials, login
from photo_keeper.backup import run_chain
from photo_keeper.config import ConfigError, load_config
from photo_keeper.errors import KeeperError

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="photo-keeper", description="Configurable backup chains for photos and other media."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="TOML configuration (default: config.toml)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check-config", help="Validate the chain configuration")
    commands.add_parser("list-chains", help="List configured chains and their enabled status")
    auth = commands.add_parser("login", help="Authorize Dropbox access for future unattended runs")
    auth.add_argument("provider", choices=["dropbox"])
    auth.add_argument("--app-key", default=os.environ.get("DROPBOX_APP_KEY"))
    auth.add_argument("--credentials-file", type=Path, help="Override where credentials are saved")
    auth.add_argument(
        "--no-browser", action="store_true", help="Print the login URL without opening it"
    )
    run = commands.add_parser("run", help="Copy a chain once and exit")
    run.add_argument("chain", help="Name of the configured chain")
    run.add_argument(
        "--dry-run", action="store_true", help="Inspect destinations without writing files"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        return execute(args)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    except (DropboxException, RequestException) as exc:
        logger.error(
            "Dropbox request failed (%s). Check connectivity and login.", type(exc).__name__
        )
        return 1
    except (KeeperError, OSError) as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.error("Interrupted.")
        return 130


def execute(args: argparse.Namespace) -> int:
    if args.command == "login":
        if not args.app_key:
            raise ConfigError(
                "Supply --app-key or set DROPBOX_APP_KEY (from the Dropbox App Console)."
            )
        if args.credentials_file is not None:
            credentials_file = args.credentials_file.expanduser().resolve()
        elif args.config.exists():
            credentials_file = load_config(args.config).credentials_file
        else:
            credentials_file = args.config.resolve().parent / ".secrets/dropbox.json"
        email = login(args.app_key, credentials_file, open_browser=not args.no_browser)
        print(f"Logged in to Dropbox as {email}. Credentials saved to {credentials_file}.")
        return 0

    config = load_config(args.config)

    if args.command == "check-config":
        print(f"Configuration valid: {len(config.chains)} chain(s).")
        return 0

    if args.command == "list-chains":
        for chain in config.chains:
            print(f"{chain.name}\t{'enabled' if chain.enabled else 'disabled'}")
        return 0

    chain = next((chain for chain in config.chains if chain.name == args.chain), None)
    if chain is None:
        logger.error("Unknown chain: %s", args.chain)
        return 2
    if not chain.enabled:
        logger.error("Chain %r is disabled.", chain.name)
        return 2

    with create_client(load_credentials(config.credentials_file)) as client:
        result = run_chain(client, chain, dry_run=args.dry_run)
    print(
        f"{'Dry run' if args.dry_run else 'Run'} {chain.name}: "
        f"copied={result.copied}, unchanged={result.unchanged}, planned={result.planned}, "
        f"ignored={result.ignored}, failed={result.failed}."
    )
    return 1 if result.failed else 0
