from __future__ import annotations

import argparse
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

WOLFCRAIG_REPO = Path("/usr/local/src/wolfcraig")
GHOST_DOCKER_REPO = Path("/usr/local/src/ghost-docker")


def clone_or_pull(url: str, dest: Path) -> None:
    if (dest / ".git").exists():
        log.info("Pulling %s", dest)
        subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"], check=True)
    else:
        log.info("Cloning %s → %s", url, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", url, str(dest)], check=True)


def setup_wolfcraig(args: argparse.Namespace) -> None:
    clone_or_pull("git@github.com:adamamyl/wolfcraig.git", WOLFCRAIG_REPO)
    clone_or_pull("git@github.com:adamamyl/ghost-docker.git", GHOST_DOCKER_REPO)

    setup_script = WOLFCRAIG_REPO / "server_setup.py"
    cmd = ["/usr/bin/python3", str(setup_script)]

    if getattr(args, "dry_run", False):
        cmd.append("--dry-run")
    if getattr(args, "verbose", False):
        cmd.append("--verbose")
    if getattr(args, "debug", False):
        cmd.append("--debug")
    if getattr(args, "quiet", False):
        cmd.append("--quiet")
    if getattr(args, "force", False):
        cmd.append("--force")

    subprocess.run(cmd, check=True)
