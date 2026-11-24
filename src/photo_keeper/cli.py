"""Command-line authentication, configuration inspection, and one-shot copies."""

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from photo_keeper.backup import run_chain
from photo_keeper.config import ConfigError, load_config
from photo_keeper.errors import KeeperError
from photo_keeper.rclone import Rclone

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
    commands.add_parser("login", help="Open interactive rclone remote setup")
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
    except (KeeperError, OSError) as exc:
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logger.error("Interrupted.")
        return 130


def execute(args: argparse.Namespace) -> int:
    if args.command == "login":
        config_file = load_config(args.config).rclone_config if args.config.exists() else None
        Rclone(config_file).configure()
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

    result = run_chain(Rclone(config.rclone_config), chain, dry_run=args.dry_run)
    print(
        f"{'Dry run' if args.dry_run else 'Run'} {chain.name}: "
        f"copied={result.copied}, unchanged={result.unchanged}, planned={result.planned}, "
        f"failed={result.failed}."
    )
    return 1 if result.failed else 0
