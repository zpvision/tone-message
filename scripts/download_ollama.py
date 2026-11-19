"""Download the official stable release with ranges and verify its published SHA256."""
import concurrent.futures
import hashlib
from pathlib import Path
import subprocess
import tempfile

URL = "https://github.com/ollama/ollama/releases/download/v0.40.5/ollama-linux-amd64.tar.zst"
SIZE = 1438684604
SHA256 = "dc5eabc292f9949546c90a97c820f924b035e05243b0de13c7f366182562169c"
TARGET = Path("/opt/finkoper-ai/downloads/ollama-0.40.5.tar.zst")
CHUNK = 16 * 1024 * 1024


def main():
    TARGET.parent.mkdir(exist_ok=True)
    if TARGET.exists():
        raise SystemExit("Archive exists; inspect it before downloading again")
    with tempfile.TemporaryDirectory(prefix="ollama-parts-", dir=TARGET.parent) as work:
        parts = []
        for number, start in enumerate(range(0, SIZE, CHUNK)):
            parts.append((Path(work) / str(number), start, min(start + CHUNK, SIZE) - 1))

        def fetch(part):
            path, start, end = part
            subprocess.run(["curl", "-fLsS", "--retry", "3", "--connect-timeout", "10",
                            "--max-time", "180", "--range", f"{start}-{end}",
                            "--output", str(path), URL], check=True)
            if path.stat().st_size != end - start + 1:
                raise RuntimeError("Invalid HTTP range length")
            return path.stat().st_size

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            for future in concurrent.futures.as_completed([pool.submit(fetch, p) for p in parts]):
                completed += future.result()
                print(f"Downloaded {completed}/{SIZE} bytes", flush=True)
        digest = hashlib.sha256()
        with TARGET.open("xb") as destination:
            for path, _, _ in parts:
                with path.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                        destination.write(block)
        if digest.hexdigest() != SHA256:
            raise RuntimeError("SHA256 mismatch: do not install this archive")
    print(f"SHA256 verified: {SHA256}", flush=True)


if __name__ == "__main__":
    main()
