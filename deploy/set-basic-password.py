"""Read the temporary password from SSH stdin, never argv/files/logs."""
import grp
import os
from pathlib import Path
import subprocess
import sys

if os.geteuid() != 0:
    raise SystemExit("Run as root")
password = sys.stdin.read().rstrip("\r\n")
if len(password) < 24 or "\n" in password:
    raise SystemExit("Invalid temporary password")
result = subprocess.run(["openssl", "passwd", "-6", "-stdin"],
                        input=password + "\n", text=True, capture_output=True, check=True)
path = Path("/etc/nginx/finkoper-ai.htpasswd")
path.write_text("tester:" + result.stdout.strip() + "\n")
os.chmod(path, 0o640)
os.chown(path, 0, grp.getgrnam("www-data").gr_gid)
print("Temporary Basic Auth hash updated")
