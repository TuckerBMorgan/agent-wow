"""Harbor EC2 environment with the benchmark worker's first-boot setup."""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
from typing import Any

import yaml
from harbor.environments.ec2 import EC2Environment as HarborEC2Environment


class EC2Environment(HarborEC2Environment):
    def __init__(self, *args: Any, user_data: str, stream_dir: str | None = None,
                 stream_world_image: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._user_data = user_data
        self._stream_dir = None
        self._character_poll = None
        if stream_dir and any(path.name == "stream.compose.yaml" for path in self.extra_docker_compose_paths):
            if not stream_world_image or self.launch_mode != "ephemeral":
                raise ValueError("Streaming requires a mod-watch world image and an ephemeral worker.")
            self._stream_dir = Path(stream_dir)
            if not self._stream_dir.is_dir() or self._stream_dir.stat().st_mode & 0o077:
                raise ValueError("Streaming requires an active private viewer session directory.")
            self._stream_world_image = stream_world_image
            self._watcher_password = secrets.token_hex(8)
            cloud_config = yaml.safe_load(user_data)
            cloud_config["runcmd"].append(
                "(snap list amazon-ssm-agent || snap install amazon-ssm-agent --classic) "
                "&& snap start amazon-ssm-agent"
            )
            self._user_data = "#cloud-config\n" + yaml.safe_dump(cloud_config)

    def _compose_env_vars(self) -> dict[str, str]:
        env = super()._compose_env_vars()
        if self._stream_dir:
            env.update(HARBOR_WATCH_WORLD_IMAGE=self._stream_world_image,
                       HARBOR_WATCHER_PASSWORD=self._watcher_password)
        return env

    def _run_instances_kwargs(self) -> dict[str, Any]:
        kwargs = super()._run_instances_kwargs()
        # Harbor 0.25.0 does not expose EC2 UserData as a built-in option.
        kwargs["UserData"] = self._user_data
        kwargs["MetadataOptions"] = {
            "HttpTokens": "required",
            "HttpPutResponseHopLimit": 1,
        }
        if self._stream_dir:
            instance_tags = next(spec["Tags"] for spec in kwargs["TagSpecifications"]
                                 if spec["ResourceType"] == "instance")
            instance_tags[:] = [tag for tag in instance_tags if tag["Key"] != "ViewerEnabled"]
            instance_tags.append({"Key": "ViewerEnabled", "Value": "true"})
        for mapping in kwargs.get("BlockDeviceMappings", []):
            mapping["Ebs"]["Encrypted"] = True
        return kwargs

    async def start(self, force_build: bool) -> None:
        await super().start(force_build)
        if self._stream_dir is None:
            return
        result = await self.service_exec(
            'AGENT_WOW_CONFIG_DIR=$(mktemp -d) || exit 1; export AGENT_WOW_CONFIG_DIR; '
            'export AGENT_WOW_DATA_DIR="$AGENT_WOW_CONFIG_DIR/data"; '
            'trap \'rm -rf "$AGENT_WOW_CONFIG_DIR"\' EXIT; '
            f"printf '%s\\n' WATCHER {shlex.quote(self._watcher_password)} | agent-wow auth init >/dev/null || exit 1; "
            'attempt=0; until agent-wow auth login --timeout 5s >/dev/null && '
            'agent-wow char create --name Observer --race orc --class warrior --timeout 10s >/dev/null; do '
            'attempt=$((attempt + 1)); [ "$attempt" -lt 12 ] || exit 1; sleep 3; done; '
            # A successful public login establishes that the async account insert is committed.
            'mkdir -m 0777 -p /viewer-bootstrap/watcher && touch /viewer-bootstrap/watcher/registered || exit 1; '
            'attempt=0; until [ -f /viewer-bootstrap/watcher/rank-set ]; do '
            'attempt=$((attempt + 1)); [ "$attempt" -lt 45 ] || exit 1; sleep 1; done',
            service="observer", timeout_sec=270,
        )
        if result.return_code != 0:
            raise RuntimeError("Could not create the spectator character and confirm its camera permission.")
        worker = self._stream_dir / "worker.tmp"
        with os.fdopen(os.open(worker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            json.dump({"instance": self.instance_id, "password": self._watcher_password}, file)
        worker.replace(self._stream_dir / "worker.json")
        self._character_poll = asyncio.create_task(self._find_stream_character())

    async def _find_stream_character(self) -> None:
        while True:
            try:
                result = await self._compose_exec(
                    ["exec", "-T", "main", "bash", "-lc",
                     "LC_ALL=C grep -aEho 'Created character: [A-Za-z]{2,12}([^A-Za-z]|$)' "
                     "/logs/agent/codex.txt /logs/agent/claude-code.txt 2>/dev/null | head -100"],
                    timeout_sec=15,
                )
                names = sorted(set(re.findall(r"Created character: ([A-Za-z]{2,12})\b", result.stdout or "")))
                if names:
                    path = self._stream_dir / "characters.tmp"
                    path.write_text(json.dumps(names))
                    path.replace(self._stream_dir / "characters.json")
                    return
            except Exception:
                self.logger.debug("Could not read character creation records for the spectator.")
            await asyncio.sleep(5)

    async def stop(self, delete: bool) -> None:
        try:
            if self._character_poll:
                self._character_poll.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._character_poll
            if self._stream_dir:
                (self._stream_dir / "stopped").touch()
        finally:
            await super().stop(delete)
