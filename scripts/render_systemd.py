"""Render the triage service for the current deployment user and repository."""

import argparse
import getpass
from pathlib import Path
import re


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parent.parent
    user = getpass.getuser()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*[$]?", user):
        parser.error("deployment user contains unsupported systemd characters")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(repo)):
        parser.error("repository path contains unsupported systemd characters")
    template = (repo / "deploy/systemd/gmail-agent-triage.service").read_text()
    service = template.replace("@RUN_USER@", user).replace("@REPO_ROOT@", str(repo))
    if "@RUN_USER@" in service or "@REPO_ROOT@" in service:
        parser.error("unresolved service template placeholder")
    args.output.write_text(service)


if __name__ == "__main__":
    main()
