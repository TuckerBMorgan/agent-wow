"""Run one level-10 Harbor trial with the existing Windows/OBS spectator."""

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


def stage_viewer_assets(source, destination):
    destination.mkdir()
    for name in ("prepare-image.ps1", "client.torrent", "obs.ps1"):
        (destination / name).symlink_to(source / name)

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
    (destination / "windows.ps1").write_text(script)

    script = (source / "watch.ps1").read_text()
    original_wait = (
        r"while ((Get-Content C:\viewer\play.log -Raw -ErrorAction SilentlyContinue) "
        r"-notmatch 'Character entry requested\.') { Start-Sleep 5 }"
        "\nStart-Sleep 30\n$current=''\nwhile (Get-Process Wow -ErrorAction SilentlyContinue) {"
    )
    script = replace_viewer_block(script, original_wait, r"""$current=''
$target=$null
$cameraEntry=$null
$cameraPrepared=$false
while ($true) {
    $game=Get-Process Wow -ErrorAction SilentlyContinue | Select-Object -First 1
    $entry=Get-Item C:\viewer\character-entry.flag -ErrorAction SilentlyContinue
    if (!$game -or !$entry -or $entry.LastWriteTimeUtc -gt [DateTime]::UtcNow.AddSeconds(-30) -or
        (Get-Content $entry.FullName -ErrorAction SilentlyContinue) -ne [string]$game.Id) {
        Start-Sleep 5
        continue
    }""")
    original_request = """    if ($downloaded) {
        $request=Get-Content C:\\viewer\\watch-request.tmp | ConvertFrom-Json
        if ($request.id -ne $current) {
            $current=$request.id
            $status=@{request_id=$current;"""
    script = replace_viewer_block(script, original_request, r"""    $request=if ($downloaded) { Get-Content C:\viewer\watch-request.tmp | ConvertFrom-Json }
    $newRequest=$request -and $request.id -ne $current
    if ($newRequest) { $current=$request.id }
    # Expiry applies when accepting a new request. Restore the last accepted target after login.
    if (!$newRequest) { $request=$target }
    if ($request) {
        if ($newRequest -or $cameraEntry -ne $entry.LastWriteTimeUtc) {
            $status=@{request_id=$request.id;""")
    script = replace_viewer_block(
        script,
        """                if (!$request.expires_at -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $request.expires_at) { throw 'Watch request expired.' }
                $game=Get-Process Wow | Select-Object -First 1""",
        """                if ($newRequest -and (!$request.expires_at -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $request.expires_at)) { throw 'Watch request expired.' }""",
    )
    script = replace_viewer_block(
        script, "                $status.state='submitted'",
        "                $target=$request\n                $cameraEntry=$entry.LastWriteTimeUtc\n                $status.state='submitted'",
    )
    # The first chat command can arrive during the new spectator's race cinematic.
    chat = "                $shell.SendKeys('{ENTER}')\n                $shell.SendKeys('.watch ' + $request.character)"
    script = replace_viewer_block(script, chat, """                if (!$cameraPrepared) {
                    $shell.SendKeys('{ESC}')
                    Start-Sleep -Milliseconds 400
                    $shell.SendKeys('{ESC}')
                    Start-Sleep -Milliseconds 600
                    $cameraPrepared=$true
                }
""" + chat)
    (destination / "watch.ps1").write_text(script)


def load_viewer(source, session):
    revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
    if revision != VIEWER_REVISION:
        raise ValueError(f"Check out viewer revision {VIEWER_REVISION} before running this integration.")
    spec = importlib.util.spec_from_file_location("agent_wow_viewer", source / "viewer.py")
    viewer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(viewer)
    stage_viewer_assets(source / "viewer", session / "viewer")
    (session / "tmp").mkdir()
    viewer.ROOT = session
    viewer.REGION = REGION
    # The old CLI's Batch launcher is no longer needed. Reuse its Windows and OBS APIs.
    viewer.agent = sys.modules[__name__]
    return viewer


aws_request = aws


def wait_for_trial(trial, session, check, timeout, error):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if trial.poll() is not None or (session / "stopped").exists():
            raise RuntimeError("The Harbor game environment ended before the viewer could connect.")
        result = check()
        if result:
            return result
        time.sleep(5)
    raise RuntimeError(error)


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
    return json.loads(path.read_text()) if path.is_file() else None


def finish_trial(trial, session):
    if trial.poll() is None and not (session / "stopped").exists():
        # Harbor handles SIGINT and performs normal worker cleanup.
        with contextlib.suppress(ProcessLookupError):
            trial.send_signal(signal.SIGINT)
    trial.wait()


def run(args, session):
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
    viewer = load_viewer(args.viewer_dir.resolve(), session)
    config = {"results_bucket": args.bucket, "viewer_launch_template": args.viewer_template,
              "viewer_subnet": args.viewer_subnet, "viewer_subnets": [args.viewer_subnet, *args.fallback_subnet]}
    account = aws(REGION, "sts", "get-caller-identity", "--query", "Account")
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
    images = [image for image in viewer.client_images(recipe) if image["state"] == "available"]
    if not images:
        raise RuntimeError("No compatible Windows viewer AMI is available in this account.")
    if viewer.instances("agent-wow-viewer"):
        raise RuntimeError("A Windows viewer is already running. Wait for its current session to finish.")
    bootstrap = viewer.bootstrap(config, "windows.ps1", recipe)
    viewer_id = None
    viewer_token = uuid.uuid4().hex
    trial = None
    job_id = str(uuid.uuid4())
    account_path = f"viewer/{job_id}/account.json"
    uploaded_account = False
    account_version = None
    try:
        viewer_id = viewer.start_gpu(config, bootstrap, max(images, key=lambda image: image["created"])["id"],
                                     extra=["--client-token", viewer_token])
        print(f"Windows viewer: {viewer_id}", flush=True)
        viewer.wait_ready(viewer_id, config)
        viewer.wait_obs(viewer_id, config)
        harness = "job.yaml" if args.harness == "codex" else "claude.yaml"
        job_name = f"stream-level-10-{args.race}-{args.character_class}-{job_id[:8]}"
        trial = subprocess.Popen([
            harbor, "run", "-c", str(ROOT / harness), "-c", str(ROOT / "aws.yaml"),
            "--path", str(task), "--jobs-dir", str(ROOT / "jobs"), "--job-name", job_name,
            "--n-attempts", "1", "--n-concurrent", "1", "--max-retries", "0",
            "--extra-docker-compose", str(ROOT / "aws/stream.compose.yaml"),
            "--environment-kwarg", f"stream_dir={session}",
            "--environment-kwarg", f"stream_world_image={args.world_image}",
        ], cwd=ROOT.parent, start_new_session=True)
        worker = wait_for_trial(trial, session, lambda: read_json(session / "worker.json"), 1800,
                                "The Harbor worker did not publish its spectator endpoint.")
        wait_for_trial(trial, session, lambda: aws(
            REGION, "ssm", "describe-instance-information", "--filters", f"Key=InstanceIds,Values={worker['instance']}",
            "--query", "InstanceInformationList[].PingStatus") == ["Online"], 120,
            "The Harbor worker did not register with Systems Manager.")
        private_file = session / "worker.json"
        private_file.write_text(json.dumps({"username": "WATCHER", "password": worker["password"],
                                           "character": "Observer"}))
        uploaded_account = True
        account_version = aws(REGION, "s3api", "put-object", "--bucket", args.bucket, "--key", account_path,
                              "--body", str(private_file), "--expected-bucket-owner", account, "--query", "VersionId")
        private_file.unlink()
        request = session / "attach.json"
        request.write_text(json.dumps({"job_id": job_id, "game_instance": worker["instance"]}))
        viewer.upload(request, args.bucket, f"viewer/{viewer_id}/job.json")
        print(f"Spectator assigned to Harbor worker {worker['instance']}.", flush=True)
        if args.watch_character:
            character = args.watch_character
        else:
            names = wait_for_trial(trial, session, lambda: read_json(session / "characters.json"), 1800,
                                   "No character creation was seen in the Harbor agent logs.")
            if len(names) != 1:
                raise RuntimeError("The agent created multiple characters. Specify --watch-character.")
            character = names[0]
        viewer.watch_request(viewer_id, config, character)
        viewer.stream_request(viewer_id, config, "test" if args.twitch_test else "live", args.channel)
        if not args.twitch_test:
            print(f"Broadcasting {character}: https://www.twitch.tv/{args.channel.lower()}", flush=True)
        while trial.poll() is None and not (session / "stopped").exists():
            time.sleep(5)
    finally:
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
    return trial.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--race", required=True)
    parser.add_argument("--class", dest="character_class", required=True)
    parser.add_argument("--harness", choices=("codex", "claude"), default="codex")
    parser.add_argument("--viewer-dir", type=Path, default=ROOT.parent.parent / "agent-wow-viewer")
    parser.add_argument("--bucket", required=True, help="Private results bucket for the existing Windows viewer.")
    parser.add_argument("--viewer-template", required=True, help="Existing Windows GPU launch template ID.")
    parser.add_argument("--viewer-subnet", required=True)
    parser.add_argument("--fallback-subnet", action="append", default=[])
    parser.add_argument("--world-image", required=True, help="Existing mod-watch worldserver image in ECR.")
    parser.add_argument("--channel", required=True, help="Twitch login with a saved AWS Secrets Manager key.")
    parser.add_argument("--twitch-test", action="store_true", help="Send a private 60-second Twitch Inspector test.")
    parser.add_argument("--watch-character", help="Use this character name instead of waiting for an agent creation record.")
    args = parser.parse_args()
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
