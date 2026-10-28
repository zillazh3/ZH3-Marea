"""Elige GPU o CPU con una prueba real al arrancar, no solo `is_available()`: que la build de torch traiga el kernel de esa GPU y
que un matmul fp16 dé el resultado correcto. Si algo falla devuelve CPU con el motivo, para mostrarlo tal cual."""


def pick_device(torch=None) -> dict:
    if torch is None:
        import torch
    info = {"device": "cpu", "name": "", "capability": None, "total_gb": 0.0, "reason": ""}
    try:
        if not torch.cuda.is_available():
            info["reason"] = "torch no ve ninguna GPU CUDA."
            return info
        cap = tuple(torch.cuda.get_device_capability(0))
        info.update(name=torch.cuda.get_device_name(0), capability=list(cap),
                    total_gb=round(torch.cuda.get_device_properties(0).total_memory / 2**30, 2))
        if f"sm_{cap[0]}{cap[1]}" not in torch.cuda.get_arch_list():
            info["reason"] = f"Esta build de torch no incluye kernels para sm_{cap[0]}{cap[1]}."
            return info
        a = torch.ones(64, 64, device="cuda", dtype=torch.float16)
        got = float((a @ a).float().sum().item())
        if got != 64.0 ** 3:
            info["reason"] = f"La GPU dio un resultado incorrecto en la prueba fp16 ({got} en vez de {64 ** 3})."
            return info
        info["device"] = "cuda"
    except Exception as e:  # driver viejo, DLL que falta, falta de VRAM…: se muestra el motivo y se sigue en CPU
        info["reason"] = f"{type(e).__name__}: {e}"
    return info
