"""假 ESP32-CAM：提供 /status 和 /capture，用于在无硬件时验证 capture.ps1。"""
import http.server
import json
import os

# 自备一张 JPEG 作为 mock 帧；无文件时回退到占位字节（请勿使用含人脸的真实照片）
IMG_PATH = os.environ.get("MOCK_IMG_PATH", "path/to/test.jpg")
IMG = open(IMG_PATH, "rb").read() if os.path.exists(IMG_PATH) else b"\xff\xd8" + b"x" * 8000
COUNT = {"n": 0}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/status"):
            b = json.dumps({"cam_ok": True, "ip": "127.0.0.1:8899", "rssi": -48,
                            "uptime_s": 12, "frames": COUNT["n"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path.startswith("/capture"):
            COUNT["n"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(IMG)))
            self.end_headers()
            self.wfile.write(IMG)
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    print("mock cam on http://127.0.0.1:8899", flush=True)
    http.server.HTTPServer(("127.0.0.1", 8899), H).serve_forever()
