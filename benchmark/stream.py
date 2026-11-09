"""Stream a new level-10 Harbor trial or attach to a spectator-enabled batch."""

import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from generate import generate


ROOT = Path(__file__).resolve().parent
VIEWER_REVISION = "9ed637708f5f454ab65e5303e4a2d254c35e99d0"
REGION = "us-west-2"


class AwsError(RuntimeError):
    pass


def aws(region, *args):
    result = subprocess.run(["aws", *args, "--region", region, "--output", "json", "--no-cli-pager"],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise AwsError(result.stderr.strip())
    return json.loads(result.stdout) if result.stdout.strip() else None


def replace_viewer_block(script, original, replacement):
    if script.count(original) != 1:
        raise RuntimeError("The pinned viewer script layout has changed.")
    return script.replace(original, replacement, 1)


def stage_viewer_assets(source, destination, first_boot=False):
    destination.mkdir()
    for name in ("prepare-image.ps1", "client.torrent"):
        (destination / name).symlink_to(source / name)

    obs = (source / "obs.ps1").read_text()
    request_failed = '    if (!$reply.d.requestStatus.result) { throw "OBS $Type failed (code $($reply.d.requestStatus.code))." }'
    # The scene startup loop retries when the OBS frontend is still loading.
    obs = replace_viewer_block(
        obs, request_failed,
        "    if ($Type -eq 'GetSceneList' -and $reply.d.requestStatus.code -eq 207) { return }\n" + request_failed,
    )
    (destination / "obs.ps1").write_text(obs)

    # A cold GPU launch can need a second window check after the renderer starts.
    script = (source / "windows.ps1").read_text()
    register_client = "Register-ScheduledTask 'AgentWowClient' -Action $action"
    script = replace_viewer_block(
        script, register_client,
        "$clientSettings=New-ScheduledTaskSettingsSet -RestartCount 2 "
        "-RestartInterval (New-TimeSpan -Minutes 1)\n" + register_client + " -Settings $clientSettings",
    )
    loop_start = script.index("$current=''; $tunnels=@()\n")
    loop_end = script.index("\nStop-Computer -Force\n'@ | Set-Content", loop_start)
    recovery = (ROOT / "aws/viewer-recovery.ps1").read_text().rstrip()
    script = script[:loop_start] + recovery + script[loop_end:]
    entry = r"Add-Content C:\viewer\play.log 'Character entry requested.'"
    script = replace_viewer_block(script, entry, entry + r"; Set-Content C:\viewer\character-entry.flag $p.Id")
    if first_boot:
        account_setup = script[script.index("# A short-lived desktop user;"):script.index("# Access is authenticated")]
        script = replace_viewer_block(script, account_setup, """# Windows Setup has already created the instance-local user.
if (!(Get-LocalUser Viewer -ErrorAction SilentlyContinue)) { throw 'First-boot viewer account is missing.' }
for ($attempt = 0; $attempt -lt 90; $attempt++) {
    $desktop = Get-Process explorer -IncludeUserName -ErrorAction SilentlyContinue |
        Where-Object { $_.UserName -like '*\\Viewer' }
    if ($desktop) { break }
    Start-Sleep 2
}
if (!$desktop) { throw 'Viewer did not sign in on the first boot.' }

""")
        script = replace_viewer_block(script, "Viewer configured from the saved image; rebooting into its private desktop.",
                                      "Viewer signed in on first boot; starting its desktop tasks.")
        script = replace_viewer_block(script, "Restart-Computer -Force", """Restart-Service dcvserver
foreach ($task in 'AgentWowTunnels', 'AgentWowClient', 'AgentWowOBS', 'AgentWowWatch') {
    Start-ScheduledTask -TaskName $task
}""")
    addon = (ROOT / "aws/viewer-watch.lua").read_text().rstrip()
    install_addon = r"""$watchAddon = Join-Path $game 'Interface\AddOns\AgentWowWatch'
New-Item -ItemType Directory -Force $watchAddon | Out-Null
@'
## Interface: 30300
## Title: Agent WoW Spectator
AgentWowWatch.lua
'@ | Set-Content (Join-Path $watchAddon 'AgentWowWatch.toc') -Encoding Ascii
@'
""" + addon + "\n'@ | Set-Content (Join-Path $watchAddon 'AgentWowWatch.lua') -Encoding Ascii\n\n"
    marker = "# Access is authenticated"
    script = replace_viewer_block(script, marker, install_addon + marker)
    (destination / "windows.ps1").write_text(script)
    (destination / "watch.ps1").symlink_to(ROOT / "aws/viewer-watch.ps1")


def load_viewer(source, session, first_boot=False):
    revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
    if revision != VIEWER_REVISION:
        raise ValueError(f"Check out viewer revision {VIEWER_REVISION} before running this integration.")
    spec = importlib.util.spec_from_file_location("agent_wow_viewer", source / "viewer.py")
    viewer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(viewer)
    stage_viewer_assets(source / "viewer", session / "viewer", first_boot)
    (session / "tmp").mkdir()
    viewer.ROOT = session
    viewer.REGION = REGION
    # The old CLI's Batch launcher is no longer needed. Reuse its Windows and OBS APIs.
    viewer.agent = sys.modules[__name__]
    return viewer


aws_request = aws


def wait_for_trial(require_trial, check, timeout, error):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        require_trial()
        result = check()
        if result:
            return result
        time.sleep(5)
    raise RuntimeError(error)


def request_watch(viewer, session, bucket, instance, character):
    if not re.fullmatch(r"[A-Za-z]{2,12}", character):
        raise ValueError("Use a character name containing 2–12 letters.")
    request = {"id": uuid.uuid4().hex, "character": character, "expires_at": int(time.time()) + 300}
    path = session / "tmp/watch-request.json"
    path.write_text(json.dumps(request))
    viewer.upload(path, bucket, f"viewer/{instance}/watch-request.json")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status = viewer.read_status(bucket, f"viewer/{instance}/watch-status.json")
        if status and status.get("request_id") == request["id"]:
            if status.get("state") == "failed":
                raise RuntimeError(f'Camera request failed: {status.get("error")}')
            if status.get("state") == "watching":
                print(f"Game confirmed the spectator is following {character}.", flush=True)
                return
        time.sleep(5)
    raise RuntimeError(f"The viewer did not confirm the camera was following {character}.")


def verify_world_image(image, account):
    match = re.fullmatch(
        rf"{account}\.dkr\.ecr\.{REGION}\.amazonaws\.com/([a-z0-9._/-]+)(?:@sha256:([a-f0-9]{{64}})|:([\w.-]+))",
        image,
    )
    if not match:
        raise ValueError(f"Use a tagged or digest-pinned world image in this account's {REGION} ECR registry.")
    repository, digest, tag = match.groups()
    arn = f"arn:aws:ecr:{REGION}:{account}:repository/{repository}"
    worker_repository = aws(REGION, "cloudformation", "describe-stacks", "--stack-name", "agent-wow-harbor",
                            "--query", "Stacks[0].Parameters[?ParameterKey=='ViewerWorldRepositoryArn'].ParameterValue | [0]")
    if worker_repository != arn:
        raise RuntimeError(f"Update the agent-wow-harbor stack with ViewerWorldRepositoryArn={arn} first.")
    image_id = f"imageDigest=sha256:{digest}" if digest else f"imageTag={tag}"
    aws(REGION, "ecr", "describe-images", "--repository-name", repository, "--image-ids", image_id,
        "--query", "imageDetails[0].imageDigest")


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None


def active_spectator(root, name):
    if not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError("Use the private spectator directory from the Harbor launch.")
    if name and (Path(name).name != name or name in (".", "..")):
        raise ValueError("Specify the Harbor trial name, without a path.")
    candidates = [root / name] if name else sorted(root.iterdir())
    active = []
    for candidate in candidates:
        if candidate.is_dir() and (worker := read_json(candidate / "worker.json")):
            active.append((candidate, worker))
    if not active:
        raise RuntimeError("No matching trial is ready for spectators. Try again while the trial is running.")
    if len(active) > 1:
        raise ValueError("Specify --trial with one of: " + ", ".join(path.name for path, _ in active))
    return active[0]


def finish_trial(trial, session):
    if trial.poll() is None and not (session / "stopped").exists():
        # Harbor handles SIGINT and performs normal worker cleanup.
        with contextlib.suppress(ProcessLookupError):
            trial.send_signal(signal.SIGINT)
    trial.wait()


def publish_twitch_status(marker, trial_name, channel, status):
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"trial": trial_name, "channel": channel, "updated_at": status["updated_at"]}))
    temporary.replace(marker)


def single_trial_command(args, session, job_id):
    harbor = Path(sys.executable).with_name("harbor")
    if not harbor.is_file():
        raise RuntimeError("Run stream.py with the benchmark project's Python environment.")
    if args.harness == "claude" and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        raise RuntimeError("Set CLAUDE_CODE_OAUTH_TOKEN before launching a Claude trial.")
    characters = json.loads((ROOT / "characters.json").read_text())
    if args.character_class not in characters.get(args.race, []):
        raise ValueError("No matching race/class combination.")
    dataset = ROOT / "tasks-level-10"
    task = dataset / f"{args.race}-{args.character_class}-level-10"
    if not (task / "task.toml").is_file():
        generate(dataset, (ROOT / "aws/image.txt").read_text().strip(), benchmark="level-10")
    harness = "job.yaml" if args.harness == "codex" else "claude.yaml"
    job_name = f"stream-level-10-{args.race}-{args.character_class}-{job_id[:8]}"
    return [
        harbor, "run", "-c", str(ROOT / harness), "-c", str(ROOT / "aws.yaml"),
        "--path", str(task), "--jobs-dir", str(ROOT / "jobs"), "--job-name", job_name,
        "--n-attempts", "1", "--n-concurrent", "1", "--max-retries", "0",
        "--extra-docker-compose", str(ROOT / "aws/stream.compose.yaml"),
        "--environment-kwarg", f"stream_dir={session}",
        "--environment-kwarg", f"stream_world_image={args.world_image}",
    ]


def run(args, session):
    job_id = str(uuid.uuid4())
    trial = None
    trial_session = session
    worker = None
    if args.attach:
        trial_session, worker = active_spectator(args.attach.resolve(), args.trial)
        running = aws(
            REGION, "ec2", "describe-instances", "--instance-ids", worker["instance"],
            "--filters", "Name=instance-state-name,Values=running", "Name=tag:ViewerEnabled,Values=true",
            f"Name=tag:harbor:session,Values={trial_session.name}__env",
            "--query", "Reservations[].Instances[].InstanceId",
        )
        if running != [worker["instance"]]:
            raise RuntimeError(f"Trial {trial_session.name} no longer has a running spectator worker.")
        print(f"Attaching to trial {trial_session.name}.", flush=True)
    else:
        command = single_trial_command(args, session, job_id)

    def trial_stopped():
        if args.attach:
            return read_json(trial_session / "worker.json") != worker
        return (trial_session / "stopped").exists() or (trial is not None and trial.poll() is not None)

    def require_trial():
        if trial_stopped():
            raise RuntimeError("The Harbor game environment ended before the viewer could connect.")

    viewer = load_viewer(args.viewer_dir.resolve(), session, first_boot=bool(args.first_boot_viewer_image))
    read_status = viewer.read_status

    def read_active_status(bucket, key):
        require_trial()
        return read_status(bucket, key)

    viewer.read_status = read_active_status
    config = {"results_bucket": args.bucket, "viewer_launch_template": args.viewer_template,
              "viewer_subnet": args.viewer_subnet, "viewer_subnets": [args.viewer_subnet, *args.fallback_subnet]}
    account = aws(REGION, "sts", "get-caller-identity", "--query", "Account")
    if not args.attach:
        verify_world_image(args.world_image, account)
    public_access_block = aws(REGION, "s3api", "get-public-access-block", "--bucket", args.bucket,
                              "--expected-bucket-owner", account, "--query", "PublicAccessBlockConfiguration")
    if not public_access_block or not all(public_access_block.get(setting) for setting in (
        "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
    )):
        raise RuntimeError("The viewer bucket must have S3 Block Public Access enabled.")
    aws(REGION, "secretsmanager", "describe-secret", "--secret-id", viewer.twitch_secret_name(args.channel),
        "--query", "ARN")
    aws(REGION, "ssm", "describe-document", "--name", "agent-wow-game-port", "--query", "Document.Name")
    recipe = viewer.client_recipe()
    if args.first_boot_viewer_image:
        images = aws(REGION, "ec2", "describe-images", "--owners", "self", "--image-ids", args.first_boot_viewer_image,
                     "--filters", "Name=state,Values=available", "Name=tag:Project,Values=agent-wow",
                     "Name=tag:ViewerFirstBoot,Values=true", f"Name=tag:ViewerBaseRecipe,Values={recipe}",
                     "--query", "Images[].ImageId")
        if images != [args.first_boot_viewer_image]:
            raise RuntimeError("The first-boot viewer image is unavailable or does not match this viewer recipe.")
        image_id = args.first_boot_viewer_image
    else:
        images = [image for image in viewer.client_images(recipe) if image["state"] == "available"]
        if not images:
            raise RuntimeError("No compatible Windows viewer AMI is available in this account.")
        image_id = max(images, key=lambda image: image["created"])["id"]
    if viewer.instances("agent-wow-viewer"):
        raise RuntimeError("A Windows viewer is already running. Wait for its current session to finish.")
    bootstrap = viewer.bootstrap(config, "windows.ps1", recipe)
    viewer_id = None
    viewer_token = uuid.uuid4().hex
    account_path = f"viewer/{job_id}/account.json"
    uploaded_account = False
    account_version = None
    viewer_starting = session / "viewer-starting"
    twitch_marker = trial_session / f"twitch-{job_id}.json" if args.attach and not args.twitch_test else None
    try:
        require_trial()
        viewer_id = viewer.start_gpu(config, bootstrap, image_id,
                                     extra=["--client-token", viewer_token])
        print(f"Windows viewer: {viewer_id}", flush=True)
        if not args.attach:
            viewer_starting.touch()
            trial = subprocess.Popen(command, cwd=ROOT.parent, start_new_session=True)
            worker = wait_for_trial(require_trial, lambda: read_json(trial_session / "worker.json"), 1800,
                                    "The Harbor worker did not publish its spectator endpoint.")
        wait_for_trial(require_trial, lambda: aws(
            REGION, "ssm", "describe-instance-information", "--filters", f"Key=InstanceIds,Values={worker['instance']}",
            "--query", "InstanceInformationList[].PingStatus") == ["Online"], 120,
            "The Harbor worker did not register with Systems Manager.")
        private_file = session / "account.json"
        with os.fdopen(os.open(private_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
            json.dump({"username": "WATCHER", "password": worker["password"], "character": "Observer"}, file)
        uploaded_account = True
        account_version = aws(REGION, "s3api", "put-object", "--bucket", args.bucket, "--key", account_path,
                              "--body", str(private_file), "--expected-bucket-owner", account, "--query", "VersionId")
        private_file.unlink()
        if not args.attach:
            (trial_session / "worker.json").unlink()
        request = session / "attach.json"
        request.write_text(json.dumps({"job_id": job_id, "game_instance": worker["instance"]}))
        viewer.upload(request, args.bucket, f"viewer/{viewer_id}/job.json")
        print(f"Spectator assigned to Harbor worker {worker['instance']}.", flush=True)
        viewer.wait_ready(viewer_id, config)
        viewer.wait_obs(viewer_id, config)
        viewer_starting.unlink(missing_ok=True)
        if args.watch_character:
            character = args.watch_character
        else:
            names = wait_for_trial(require_trial, lambda: read_json(trial_session / "characters.json"), 1800,
                                   "No character creation was seen in the Harbor agent logs.")
            if len(names) != 1:
                raise RuntimeError("The agent created multiple characters. Specify --watch-character.")
            character = names[0]
        request_watch(viewer, session, args.bucket, viewer_id, character)
        broadcast = viewer.stream_request(viewer_id, config, "test" if args.twitch_test else "live", args.channel)
        if not args.twitch_test:
            print(f"Broadcasting {character}: https://www.twitch.tv/{args.channel.lower()}", flush=True)
        channel = args.channel.lower()
        if twitch_marker and viewer.live_output(broadcast, job_id, channel):
            with contextlib.suppress(OSError):
                publish_twitch_status(twitch_marker, trial_session.name, channel, broadcast)
        next_status_check = time.monotonic() + 10
        while not trial_stopped():
            time.sleep(5)
            if twitch_marker and time.monotonic() >= next_status_check:
                next_status_check = time.monotonic() + 10
                # An unavailable status read must not interrupt the broadcast. The website expires old samples.
                with contextlib.suppress(AwsError, OSError, ValueError, subprocess.SubprocessError):
                    status = read_status(args.bucket, f"viewer/{viewer_id}/stream-status.json")
                    if status and status.get("request_id") == broadcast["request_id"] and viewer.live_output(status, job_id, channel):
                        publish_twitch_status(twitch_marker, trial_session.name, channel, status)
                    else:
                        twitch_marker.unlink(missing_ok=True)
        if args.attach:
            print(f"Trial {trial_session.name} ended. Closing its viewer.", flush=True)
    finally:
        if twitch_marker:
            with contextlib.suppress(OSError):
                twitch_marker.unlink(missing_ok=True)
        try:
            try:
                instances = [viewer_id] if viewer_id else aws(
                    REGION, "ec2", "describe-instances", "--filters", f"Name=client-token,Values={viewer_token}",
                    "--query", "Reservations[].Instances[].InstanceId")
                if instances:
                    aws(REGION, "ec2", "terminate-instances", "--instance-ids", *instances,
                        "--query", "TerminatingInstances[].InstanceId")
            finally:
                if uploaded_account:
                    version = ["--version-id", account_version] if account_version else []
                    aws(REGION, "s3api", "delete-object", "--bucket", args.bucket, "--key", account_path,
                        "--expected-bucket-owner", account, *version)
        finally:
            if trial is not None:
                finish_trial(trial, session)
    return trial.returncode if trial is not None else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--race")
    parser.add_argument("--class", dest="character_class")
    parser.add_argument("--attach", type=Path, metavar="DIR", help="Attach to an active trial in this private spectator directory.")
    parser.add_argument("--trial", help="Select a Harbor trial by name when attaching. Defaults to the only active trial.")
    parser.add_argument("--harness", choices=("codex", "claude"), default="codex")
    parser.add_argument("--viewer-dir", type=Path, default=ROOT.parent.parent / "agent-wow-viewer")
    parser.add_argument("--first-boot-viewer-image", help="Use a viewer AMI that signs in during its first Windows boot.")
    parser.add_argument("--bucket", required=True, help="Private results bucket for the existing Windows viewer.")
    parser.add_argument("--viewer-template", required=True, help="Existing Windows GPU launch template ID.")
    parser.add_argument("--viewer-subnet", required=True)
    parser.add_argument("--fallback-subnet", action="append", default=[])
    parser.add_argument("--world-image", help="Existing mod-watch worldserver image in ECR for a new trial.")
    parser.add_argument("--channel", required=True, help="Twitch login with a saved AWS Secrets Manager key.")
    parser.add_argument("--twitch-test", action="store_true", help="Send a private 60-second Twitch Inspector test.")
    parser.add_argument("--watch-character", help="Use this character name instead of waiting for an agent creation record.")
    args = parser.parse_args()
    if args.attach and (args.race or args.character_class or args.world_image):
        parser.error("--attach cannot be combined with --race, --class, or --world-image.")
    if not args.attach and (args.trial or not (args.race and args.character_class and args.world_image)):
        parser.error("Use --attach for an existing trial or specify --race, --class, and --world-image for a new one.")
    if args.watch_character and not re.fullmatch(r"[A-Za-z]{2,12}", args.watch_character):
        parser.error("Character names must contain 2–12 letters.")
    try:
        with tempfile.TemporaryDirectory(prefix="agent-wow-harbor-stream-") as directory:
            return run(args, Path(directory))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Stream failed. {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
