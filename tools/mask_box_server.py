# -*- coding: utf-8 -*-
"""手动打码工具后端:浏览抽帧图片、保存手画的框、按框模糊写回、恢复帧。

启动:
  python tools/mask_box_server.py
  然后浏览器打开 http://127.0.0.1:8021
"""
import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

FRAMES_DIR = Path(r"D:\低空经济\视频\新建文件夹")
VIDEO = Path(r"D:\低空经济\视频\完整视频_10.25_1.0 - 副本.mp4")
BOXES_PATH = Path(r"D:\低空经济\视频\_inspect\boxes.json")
HTML_PATH = Path(__file__).with_name("mask_box_editor.html")
PORT = 8021
FPS = 30.0


def list_frames():
    return sorted(p.name for p in FRAMES_DIR.glob("f_*.jpg"))


def load_boxes():
    if BOXES_PATH.exists():
        return json.loads(BOXES_PATH.read_text(encoding="utf-8"))
    return {}


def blur_frame(path: Path, rects):
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    for r in rects:
        x0 = max(0, int(round(r["x"])))
        y0 = max(0, int(round(r["y"])))
        x1 = min(w, int(round(r["x"] + r["w"])))
        y1 = min(h, int(round(r["y"] + r["h"])))
        if x1 - x0 < 3 or y1 - y0 < 3:
            continue
        roi = img[y0:y1, x0:x1]
        k = int(max(15, min(x1 - x0, y1 - y0) * float(r.get("strength", 0.35))))
        if k % 2 == 0:
            k += 1
        blurred = cv2.GaussianBlur(roi, (k, k), 0)
        blurred = cv2.GaussianBlur(blurred, (k, k), 0)
        img[y0:y1, x0:x1] = blurred
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if ok:
        buf.tofile(str(path))


def restore_frames(start: int, end: int) -> int:
    end = min(end, len(list_frames()))
    pattern = (FRAMES_DIR / "f_%05d.jpg").as_posix()
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(VIDEO),
        "-vf", f"select=between(n\\,{start - 1}\\,{end - 1})",
        "-vsync", "0", "-q:v", "1", "-start_number", str(start),
        pattern, "-y",
    ]
    subprocess.run(cmd, check=True)
    return end - start + 1


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
        elif self.path.startswith("/frames/"):
            p = FRAMES_DIR / Path(self.path).name
            if p.exists():
                self._send(200, p.read_bytes(), "image/jpeg")
            else:
                self._send(404, b"missing frame", "text/plain")
        elif self.path == "/api/state":
            self._json({"frames": list_frames(), "boxes": load_boxes(), "fps": FPS})
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            req = json.loads(body.decode("utf-8")) if n else {}
            if self.path == "/api/boxes":
                BOXES_PATH.parent.mkdir(exist_ok=True)
                BOXES_PATH.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
                self._json({"ok": True})
            elif self.path == "/api/apply":
                boxes = req.get("boxes") or load_boxes()
                strength = float(req.get("strength", 0.35))
                scope = req.get("scope", "all")
                if scope == "current":
                    wanted = {str(int(req.get("frame", 0)))}
                else:
                    wanted = set(boxes)
                done = 0
                for key in sorted(wanted, key=lambda k: int(k)):
                    rects = boxes.get(key) or []
                    if not rects:
                        continue
                    p = FRAMES_DIR / f"f_{int(key):05d}.jpg"
                    if p.exists():
                        for r in rects:
                            r["strength"] = strength
                        blur_frame(p, rects)
                        done += 1
                self._json({"ok": True, "frames": done})
            elif self.path == "/api/restore":
                start = max(1, int(req.get("start", 1)))
                end = int(req.get("end", start))
                count = restore_frames(start, end)
                self._json({"ok": True, "restored": count})
            else:
                self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc)}, 500)

    def log_message(self, *args):  # 静默访问日志
        pass


if __name__ == "__main__":
    print(f"manual mask editor: http://127.0.0.1:{PORT}")
    print(f"frames dir: {FRAMES_DIR}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
