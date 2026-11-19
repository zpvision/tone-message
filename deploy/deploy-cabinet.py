"""Deploy an already staged and tested cabinet, preserving existing infrastructure."""
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def command(*args):
    subprocess.run(args, check=True)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root")
    root = Path("/opt/finkoper-ai")
    stage = root / "stage-cabinet"
    env = Path("/etc/finkoper-ai/finkoper-ai.env")
    site = Path("/etc/nginx/sites-available/finkoper-ai")
    auth = Path("/etc/nginx/finkoper-ai.htpasswd")
    if not auth.is_file():
        raise SystemExit("Prepare restricted htpasswd with set-basic-password.py via SSH stdin first")
    backup = Path("/etc/finkoper-ai/backups") / datetime.now(timezone.utc).strftime("cabinet-%Y%m%dT%H%M%SZ")
    backup.mkdir(mode=0o700)
    shutil.copytree(root / "app", backup / "app")
    shutil.copy2(env, backup / "finkoper-ai.env")
    shutil.copy2(site, backup / "nginx-site.conf")
    shutil.copy2(root / "requirements.lock.txt", backup / "requirements.lock.txt")
    # Authentication file is provisioned separately from code/configuration.
    # Do not replace a working password as a side effect of deployment.
    values = {"FINKOPER_CABINET_ENABLED": "true", "FINKOPER_CABINET_DATA_DIR": "/var/lib/finkoper-ai/cabinet",
              "FINKOPER_OLLAMA_NUM_PARALLEL": "1"}
    metadata = env.stat()
    lines = [line for line in env.read_text().splitlines() if line.split("=", 1)[0] not in values]
    lines.extend(f"{key}={value}" for key, value in values.items())
    command("systemctl", "stop", "finkoper-ai.service")
    try:
        shutil.copytree(stage / "app", root / "app", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))
        with tempfile.NamedTemporaryFile(mode="w", dir=env.parent, delete=False) as file:
            file.write("\n".join(lines) + "\n")
            temporary = Path(file.name)
        os.chmod(temporary, metadata.st_mode & 0o777)
        os.chown(temporary, metadata.st_uid, metadata.st_gid)
        temporary.replace(env)
        shutil.copy2(stage / "deploy/nginx-cabinet.conf", site)
        command("nginx", "-t")
        command("systemctl", "start", "finkoper-ai.service")
        command("systemctl", "reload", "nginx.service")
    except Exception:
        shutil.copytree(backup / "app", root / "app", dirs_exist_ok=True)
        shutil.copy2(backup / "finkoper-ai.env", env)
        os.chown(env, metadata.st_uid, metadata.st_gid)
        shutil.copy2(backup / "nginx-site.conf", site)
        command("systemctl", "start", "finkoper-ai.service")
        command("systemctl", "reload", "nginx.service")
        raise
    print(f"Cabinet deployed. Backup: {backup}")
    # Current UFW/nft/iptables policy was inspected: no filtering, so binding
    # the authenticated Nginx port 80 needs no firewall rule changes. SSH stays.


if __name__ == "__main__":
    main()
