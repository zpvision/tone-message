from pathlib import Path

from cabinet_integration import file_bytes

target = Path("/opt/finkoper-ai/verification/cabinet-synthetic.xlsx")
target.write_bytes(file_bytes())
target.chmod(0o644)
print("Synthetic XLSX saved (8 data rows, two sheets)")
