#!/usr/bin/env python3

from http.server import (
    BaseHTTPRequestHandler,
    ThreadingHTTPServer,
)

import json
import time
import urllib.request

import cv2
import numpy as np


HOST = "127.0.0.1"
PORT = 8820

D435_RAW_URL = (
    "http://192.168.20.10:8766/raw"
)

DASHBOARD_STATE_URL = (
    "http://127.0.0.1:8811/api/state"
)

OUT_W = 1280
OUT_H = 720

ROTATE_180_VIEWS = {
    "LEFT",
    "RIGHT",
    "FRONT",
}


def expected_next():

    try:

        with urllib.request.urlopen(
            DASHBOARD_STATE_URL,
            timeout=1.0,
        ) as r:

            d = json.loads(
                r.read().decode("utf-8")
            )

        return d.get(
            "expected_next"
        )

    except Exception:

        return None


def fetch_raw():

    with urllib.request.urlopen(
        D435_RAW_URL,
        timeout=3.0,
    ) as r:

        data = r.read()

    frame = cv2.imdecode(
        np.frombuffer(
            data,
            dtype=np.uint8,
        ),
        cv2.IMREAD_COLOR,
    )

    if frame is None:

        raise RuntimeError(
            "D435 RAW decode failed"
        )

    return frame


def cover_1280x720(
    frame,
):

    h, w = frame.shape[:2]

    scale = max(
        OUT_W / w,
        OUT_H / h,
    )

    nw = int(
        round(
            w * scale
        )
    )

    nh = int(
        round(
            h * scale
        )
    )

    frame = cv2.resize(
        frame,
        (
            nw,
            nh,
        ),
        interpolation=cv2.INTER_LINEAR,
    )

    x0 = max(
        0,
        (nw - OUT_W) // 2
    )

    y0 = max(
        0,
        (nh - OUT_H) // 2
    )

    frame = frame[
        y0:y0 + OUT_H,
        x0:x0 + OUT_W
    ]

    if frame.shape[:2] != (
        OUT_H,
        OUT_W,
    ):

        frame = cv2.resize(
            frame,
            (
                OUT_W,
                OUT_H,
            ),
            interpolation=cv2.INTER_LINEAR,
        )

    return frame


def processed_jpeg():

    frame = fetch_raw()

    view = expected_next()

    if view in ROTATE_180_VIEWS:

        frame = cv2.rotate(
            frame,
            cv2.ROTATE_180,
        )

    frame = cover_1280x720(
        frame
    )

    # No text overlay.

    ok, enc = cv2.imencode(
        ".jpg",
        frame,
        [
            cv2.IMWRITE_JPEG_QUALITY,
            90,
        ],
    )

    if not ok:

        raise RuntimeError(
            "JPEG encode failed"
        )

    return enc.tobytes()


HTML = b"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html,body {
    margin:0;
    padding:0;
    width:100%;
    height:100%;
    overflow:hidden;
    background:#000;
}
img {
    position:fixed;
    inset:0;
    width:100vw;
    height:100vh;
    object-fit:cover;
    display:block;
}
</style>
</head>
<body>
<img src="/stream">
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):

    def log_message(
        self,
        fmt,
        *args,
    ):
        pass


    def do_GET(self):

        if self.path == "/":

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/html; charset=utf-8"
            )

            self.send_header(
                "Cache-Control",
                "no-store"
            )

            self.end_headers()

            self.wfile.write(
                HTML
            )

            return


        if self.path == "/frame.jpg":

            try:

                jpeg = processed_jpeg()

                self.send_response(200)

                self.send_header(
                    "Content-Type",
                    "image/jpeg"
                )

                self.send_header(
                    "Content-Length",
                    str(len(jpeg))
                )

                self.send_header(
                    "Cache-Control",
                    "no-store"
                )

                self.end_headers()

                self.wfile.write(
                    jpeg
                )

            except Exception:

                self.send_response(503)
                self.end_headers()

            return


        if self.path != "/stream":

            self.send_response(404)
            self.end_headers()

            return


        self.send_response(200)

        self.send_header(
            "Cache-Control",
            "no-store, no-cache, must-revalidate"
        )

        self.send_header(
            "Pragma",
            "no-cache"
        )

        self.send_header(
            "Content-Type",
            "multipart/x-mixed-replace; boundary=frame"
        )

        self.end_headers()


        while True:

            try:

                jpeg = processed_jpeg()

                self.wfile.write(
                    b"--frame\r\n"
                )

                self.wfile.write(
                    b"Content-Type: image/jpeg\r\n"
                )

                self.wfile.write(
                    (
                        f"Content-Length: "
                        f"{len(jpeg)}\r\n\r\n"
                    ).encode()
                )

                self.wfile.write(
                    jpeg
                )

                self.wfile.write(
                    b"\r\n"
                )

                self.wfile.flush()

                time.sleep(
                    0.05
                )

            except (
                BrokenPipeError,
                ConnectionResetError,
            ):

                return

            except Exception as e:

                print(
                    f"[WARN] {e}",
                    flush=True,
                )

                time.sleep(
                    0.2
                )


def main():

    print(
        "HARMONY D435 MOVE BRIDGE V3",
        flush=True,
    )

    print(
        "OUTPUT      : 1280x720 FULL COVER",
        flush=True,
    )

    print(
        "TOP/BEHIND  : IDENTITY",
        flush=True,
    )

    print(
        "LEFT/RIGHT/FRONT : ROTATE_180",
        flush=True,
    )

    print(
        "OVERLAY     : NONE",
        flush=True,
    )

    ThreadingHTTPServer(
        (
            HOST,
            PORT,
        ),
        Handler,
    ).serve_forever()


if __name__ == "__main__":
    main()
