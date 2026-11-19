"""Run in a separate process as the service user; loads no model."""
import gc
import json

import torch


def main():
    available = torch.cuda.is_available()
    print(json.dumps({"torch": torch.__version__, "wheel_cuda": torch.version.cuda,
                      "cuda_available": available}), flush=True)
    if not available:
        raise SystemExit("CUDA unavailable")
    device = torch.device("cuda:0")
    free, total = torch.cuda.mem_get_info(device)
    print(json.dumps({"gpu": torch.cuda.get_device_name(device),
                      "free_mib": free // 2**20, "total_mib": total // 2**20,
                      "compiled_architectures": torch.cuda.get_arch_list()}), flush=True)
    a = b = None
    try:
        a = torch.ones((256, 256), device=device)
        b = a @ a
        torch.cuda.synchronize(device)
        if not (b.is_cuda and torch.all(b == 256).item()):
            raise RuntimeError("GPU matrix multiplication result mismatch")
        print("GPU matrix multiplication: PASS", flush=True)
    finally:
        a = b = None
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.synchronize(device)
        print(json.dumps({"allocated_bytes_after_cleanup": torch.cuda.memory_allocated(device)}))
    # CUDA context disappears on process exit; check separately with nvidia-smi.


if __name__ == "__main__":
    main()
