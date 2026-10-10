#!/usr/bin/env python3
"""AI cameraman detector process (CPU only).

Runs in its OWN process, started by engine.py. The main app never imports
torch / OpenVINO / Ultralytics, so a native-library clash or crash in here can
never take the main app (GStreamer, OBS control, web server) down with it.

Protocol:
  frame   : shared memory block, analysis_height x analysis_width x 3 (BGR),
            written by the main process only while this worker is idle
  stdin   : one JSON command per line   {"type": "frame", "dt": 0.1} | {"type": "quit"}
  fd N    : one JSON reply per line     {"type": "ready", ...} | {"type": "result", ...}
            (N = $AICAM_REPLY_FD; stdout is left to Ultralytics' own logging)
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

# Thread caps MUST be set before numpy / torch / OpenVINO are imported.
_THREADS = os.environ.get("AICAM_THREADS", "2")
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
             "NUMEXPR_NUM_THREADS", "TBB_NUM_THREADS"):
    os.environ.setdefault(_var, _THREADS)
# Never pip-install or download anything at runtime (venue may be offline, and a
# tournament is no time to modify the venv). Missing deps fail loudly instead.
os.environ.setdefault("YOLO_AUTOINSTALL", "false")
os.environ.setdefault("YOLO_OFFLINE", "1")

_ROOT = Path(__file__).resolve().parents[3]          # project root (folder that holds plugins/)
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def log(msg: str) -> None:
    print(f"[aicam-worker] {msg}", file=sys.stderr, flush=True)


def detector_shape(cfg) -> tuple[int, int]:
    """(h, w) detector input: width = imgsz, height = 16:9 rounded up to the stride (640 -> 384).
    A rectangular input skips the letterbox padding: ~40% less compute than 640x640."""
    w = int(math.ceil(cfg.imgsz / 32) * 32)
    h = int(math.ceil(w * cfg.analysis_height / cfg.analysis_width / 32) * 32)
    return h, w


def _cap_openvino_threads(n: int) -> None:
    """Ultralytics creates its own openvino.Core(); make every Core cap CPU threads."""
    import openvino as ov
    if getattr(ov.Core, "_aicam_capped", None) == n:
        return
    base = getattr(ov.Core, "_aicam_base", ov.Core)

    class CappedCore(base):
        _aicam_capped = n
        _aicam_base = base

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.set_property("CPU", {"INFERENCE_NUM_THREADS": n})

    ov.Core = CappedCore


def load_detector(cfg, backend: str):
    """Returns (model, device, shape). CPU only."""
    import numpy as np
    try:
        import torch
        torch.set_num_threads(cfg.cpu_threads)
    except Exception:
        pass
    from ultralytics import YOLO

    h, w = detector_shape(cfg)
    path = Path(cfg.model_path).expanduser()
    if backend == "openvino":
        _cap_openvino_threads(cfg.cpu_threads)
        if path.suffix == ".pt":
            # The folder name MUST end in "_openvino_model": Ultralytics picks the backend from it.
            target = path.with_name(f"{path.stem}_{h}x{w}{'_fp16' if cfg.fp16 else ''}_openvino_model")
            if not (target / "metadata.yaml").exists():
                log(f"exporting {path.name} to OpenVINO {w}x{h} (one-time)…")
                out = YOLO(str(path)).export(format="openvino", imgsz=(h, w), half=cfg.fp16, dynamic=False)
                if Path(out).resolve() != target.resolve():
                    shutil.rmtree(target, ignore_errors=True)
                    shutil.move(str(out), str(target))
            path = target
        # "intel:cpu" pins OpenVINO to its CPU plugin. Plain "cpu" lets OpenVINO pick
        # AUTO, which grabs the integrated GPU.
        device = "intel:cpu"
    else:
        device = "cpu"

    model = YOLO(str(path), task="detect")
    dummy = np.zeros((cfg.analysis_height, cfg.analysis_width, 3), np.uint8)
    model.predict(dummy, imgsz=(h, w), device=device, verbose=False)       # compile + warm up
    return model, device, (h, w)


def debug_image_path(discipline: str) -> str:
    import tempfile
    return os.path.join(tempfile.gettempdir(), f"aicam_{discipline}_debug.jpg")


def _write_debug(frame, mat, boxes, ids, chosen, held, path) -> None:
    """Annotated analysis frame: mat outline, everyone detected, who is framed."""
    import cv2
    try:
        img = frame.copy()
        if mat.poly is not None:
            cv2.polylines(img, [mat.poly], True, (0, 255, 255), 2)
        for box, tid in zip(boxes, ids):
            x0, y0, x1, y1 = (int(v) for v in box)
            col = (0, 210, 0) if tid in chosen else (150, 150, 150)
            cv2.rectangle(img, (x0, y0), (x1, y1), col, 2 if tid in chosen else 1)
            cv2.putText(img, str(tid), (x0, y0 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1)
        info = mat.info or {}
        txt = (f"mat: {info.get('source')} {info.get('color') or '-'} "
               f"{info.get('corners') or '-'} corners | framed: {len(chosen)}"
               + (f" ({len(held)} held)" if held else ""))
        cv2.rectangle(img, (0, img.shape[0] - 30), (img.shape[1], img.shape[0]), (0, 0, 0), -1)
        cv2.putText(img, txt, (10, img.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1,
                    cv2.LINE_AA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(jpg.tobytes())
            os.replace(tmp, path)                  # atomic: readers never see half a file
    except Exception:
        pass


def main() -> int:
    from multiprocessing import shared_memory

    import numpy as np
    from plugins.universal.ai_cameraman import autocam
    from plugins.universal.ai_cameraman.settings import settings_from_json

    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)            # shutdown comes from the renderer
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))   # nothing to clean up here

    cfg = settings_from_json(sys.argv[1])
    shm_name, backend = sys.argv[2], sys.argv[3]
    reply = os.fdopen(int(os.environ["AICAM_REPLY_FD"]), "w", buffering=1)

    def send(obj) -> None:
        reply.write(json.dumps(obj) + "\n")

    try:
        os.nice(10)
    except OSError:
        pass
    cores = os.environ.get("AICAM_CORES")
    if cores:
        try:
            os.sched_setaffinity(0, {int(c) for c in cores.split(",")})
        except OSError as exc:
            log(f"cannot pin to cores {cores}: {exc}")

    try:
        shm = shared_memory.SharedMemory(name=shm_name, track=False)     # py >= 3.13
    except TypeError:
        # py < 3.13: stop this process's resource tracker from deleting the
        # renderer's block when we exit (it would break the next restart)
        shm = shared_memory.SharedMemory(name=shm_name)
        try:
            from multiprocessing import resource_tracker
            resource_tracker.unregister(shm._name, "shared_memory")
        except Exception:
            pass
    aw, ah = cfg.analysis_width & ~1, cfg.analysis_height & ~1
    frame = np.ndarray((ah, aw, 3), dtype=np.uint8, buffer=shm.buf)

    try:
        import cv2
        cv2.setNumThreads(1)
        model, device, shape = load_detector(cfg, backend)
    except Exception as exc:
        send({"type": "error", "error": f"{exc!r}", "trace": traceback.format_exc()})
        return 2

    nominal = cfg.detect_fps
    selector = autocam.make_selector(cfg, nominal)
    mat = autocam.make_mat(cfg, aw / cfg.input_width, nominal)
    send({"type": "ready", "device": device, "backend": backend, "shape": list(shape)})
    log(f"ready: {backend} on CPU, {shape[1]}x{shape[0]}, {cfg.cpu_threads} threads")

    idx = 0
    next_snap = 0.0
    debug_path = debug_image_path(cfg.discipline)
    for line in sys.stdin:                  # EOF (parent gone) ends the loop
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "quit":
            break
        if msg.get("type") != "frame":
            continue
        t0 = time.perf_counter()
        try:
            mat.update(frame, idx)
            res = model.track(frame, persist=True, classes=[0], conf=cfg.conf, imgsz=shape,
                              tracker="bytetrack.yaml", device=device, verbose=False)[0]
            if res.boxes is not None and res.boxes.id is not None:
                boxes = res.boxes.xyxy.cpu().numpy().tolist()
                ids = res.boxes.id.int().cpu().tolist()
            else:
                boxes, ids = [], []
            chosen_boxes, chosen, held, _on = selector.select(frame, boxes, ids, idx, mat)
            idx += 1
            now = time.monotonic()
            if now >= next_snap:                     # what the AI sees (for /debug), every 2 s
                next_snap = now + 2.0
                _write_debug(frame, mat, boxes, ids, chosen, held, debug_path)
            send({"type": "result",
                  "boxes": [b for b in chosen_boxes if b is not None],
                  "wide": mat.bbox(),
                  "chosen": chosen,
                  "mat": mat.info,
                  "infer_ms": 1000.0 * (time.perf_counter() - t0)})
        except Exception as exc:
            send({"type": "result", "boxes": [], "wide": mat.bbox(), "chosen": [],
                  "infer_ms": 0.0, "error": f"{exc!r}"})
    shm.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())