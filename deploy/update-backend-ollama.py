"""Apply the tested stage-2 app and edit only the three owned env settings."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root")
    root = Path("/opt/finkoper-ai")
    stage = root / "stage-ollama"
    environment = Path("/etc/finkoper-ai/finkoper-ai.env")
    backup = Path("/etc/finkoper-ai/backups") / datetime.now(timezone.utc).strftime("ollama-%Y%m%dT%H%M%SZ")
    backup.mkdir(mode=0o700)
    shutil.copytree(root / "app", backup / "app")
    shutil.copy2(environment, backup / "finkoper-ai.env")
    changes = {"FINKOPER_INFERENCE_URL": "http://127.0.0.1:11434",
               "FINKOPER_MODEL_ID": os.environ["FINKOPER_MODEL_ID"], "FINKOPER_READINESS_TIMEOUT_SECONDS": "2"}
    lines = []
    for line in environment.read_text().splitlines():
        key = line.split("=", 1)[0]
        if key in changes:
            continue
        lines.append(line)
    lines.extend(f"{key}={value}" for key, value in changes.items())
    metadata = environment.stat()
    subprocess.run(["systemctl", "stop", "finkoper-ai.service"], check=True)
    try:
        shutil.copytree(stage / "app", root / "app", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        with tempfile.NamedTemporaryFile(mode="w", dir=environment.parent, delete=False) as target:
            target.write("\n".join(lines) + "\n")
            temporary = Path(target.name)
        os.chmod(temporary, metadata.st_mode & 0o777)
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        temporary.replace(environment)
        subprocess.run(["systemctl", "start", "finkoper-ai.service"], check=True)
    except Exception:
        shutil.copytree(backup / "app", root / "app", dirs_exist_ok=True)
        shutil.copy2(backup / "finkoper-ai.env", environment)
        os.chown(environment, metadata.st_uid, metadata.st_gid)
        subprocess.run(["systemctl", "start", "finkoper-ai.service"], check=True)
        raise
    print(f"Backend updated; root-only backup: {backup}")


if __name__ == "__main__":
    main()
