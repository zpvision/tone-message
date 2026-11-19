"""Assert the actual HTTP behavior and process identity on the server."""
import os
import json
import subprocess
import urllib.error
import urllib.request


def request(url):
    try:
        response = urllib.request.urlopen(url, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        if response.status == 401:
            return 401, {"auth_required": True}
        return response.status, json.load(response)


for port in (8000, 8080):
    for path, expected_code in (("/healthz", 200), ("/readyz", 200)):
        code, body = request(f"http://127.0.0.1:{port}{path}")
        if port == 8080:
            assert code == 401, (port, path, code)
            print(f"port {port} {path}: Basic Auth required PASS")
            continue
        assert code == expected_code, (port, path, code, body)
        if path == "/healthz":
            assert body == {"status": "ok"}, body
        else:
            assert body["status"] == "ready", body
            assert body["inference"]["available"] is True, body
            assert body["model"]["name"] == os.environ["FINKOPER_MODEL_ID"], body
            assert body["model"]["installed"] is True, body
            assert body["model"]["loaded"] in (True, False, None), body
        print(json.dumps({"port": port, "path": path, "status": code, "body": body}))

for state in ("is-active", "is-enabled"):
    for service in ("finkoper-ai.service", "nginx.service", "ollama.service"):
        subprocess.run(["systemctl", state, service], check=True)

listeners = subprocess.check_output(["ss", "-H", "-lnt"], text=True)
for port in (8000, 8080, 11434):
    addresses = [line.split()[3] for line in listeners.splitlines()
                 if line.split()[3].endswith(f":{port}")]
    assert addresses == [f"127.0.0.1:{port}"], addresses
    print(f"port {port}: loopback only PASS")

pid = subprocess.check_output(
    ["systemctl", "show", "finkoper-ai.service", "--property=MainPID", "--value"],
    text=True,
).strip()
user = subprocess.check_output(["ps", "-o", "user=", "-p", pid], text=True).strip()
assert user == "finkoper-ai", user
children = subprocess.run(["pgrep", "-P", pid], capture_output=True, text=True)
assert children.returncode == 1, children.stdout
print(f"backend PID {pid}: user={user}, no worker child processes PASS")

ollama_user = subprocess.check_output(
    ["systemctl", "show", "ollama.service", "--property=User", "--value"], text=True
).strip()
assert ollama_user == "ollama", ollama_user
print("ollama.service: non-root user=ollama PASS")
