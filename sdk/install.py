#!/usr/bin/env python3
"""Build and install the gameplay module in a client workspace before login."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
IMAGE = "agent-wow-gameplay:local"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules-dir", type=Path,
                        default=Path(os.environ.get("AGENT_WOW_MODULE_DIR", Path.cwd() / "modules")))
    args = parser.parse_args()
    target = args.modules_dir.resolve() / "gameplay"
    subprocess.run(["docker", "build", "--build-context", f"api={HERE.parent / 'api'}",
                    "-t", IMAGE, str(HERE)], check=True)
    # The descriptor must exist on disk before the client discovers the module.
    with tempfile.TemporaryDirectory(prefix="gameplay-module-") as directory:
        descriptor = Path(directory) / "module.pb"
        container = subprocess.check_output(["docker", "create", IMAGE], text=True).strip()
        try:
            subprocess.run(["docker", "cp", container + ":/api/module.pb", str(descriptor)], check=True)
        finally:
            subprocess.run(["docker", "rm", container], check=True, stdout=subprocess.DEVNULL)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(descriptor, target / "module.pb")
        for name in ("module.yaml", "compose.yaml"):
            shutil.copyfile(HERE / name, target / name)
    print(f"Installed {target}; loads on the next character login.")
    print(f"Start the client with AGENT_WOW_MODULE_DIR={target.parent}")


if __name__ == "__main__":
    main()
