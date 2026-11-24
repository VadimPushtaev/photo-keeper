"""Command-line entry point for configuration and future scheduled runs."""

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from photo_keeper.config import ConfigError, load_config

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
    run = commands.add_parser("run", help="Run a chain (placeholder; execution is not implemented)")
    run.add_argument("chain", help="Name of the configured chain")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2

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

    logger.error("Chain execution is not implemented yet (%s).", chain.name)
    return 1
