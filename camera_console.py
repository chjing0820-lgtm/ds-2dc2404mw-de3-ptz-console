# -*- coding: utf-8 -*-
"""
摄像头控制台（本地网页版上位机）
实时视频 / 拍照 / 云台方向 / 变速 / 变倍 / 预置点

技术路线：OpenCV 读 RTSP（低延迟参数）+ Flask 输出 MJPEG + ISAPI 控制云台
云台命令走独立发送线程 + 复用 HTTP 会话，避免每条指令都重新做 Digest 握手。
"""

import datetime
import json
import os
import re
import threading
import time
import webbrowser
from urllib.parse import quote

import cv2
import numpy as np
import requests
from flask import Flask, Response, jsonify, request, send_from_directory, render_template_string
from requests.auth import HTTPDigestAuth

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except Exception:
    HAS_PIL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SHOT_DIR = os.path.join(BASE_DIR, "captures")

DEFAULT_CONFIG = {
    "device": {
        "ip": "192.168.1.64",
        "port": 80,
        "user": "admin",
        "password": "",
        "channel": 1,
        "stream": "sub"
    },
    "ptz": {"speed": 40, "invert_pan": False, "invert_tilt": False},
    "video": {"low_latency": True, "preview_width": 900, "preview_fps": 16,
              "flip": "none", "stamp": True},
    "server": {"host": "127.0.0.1", "port": 8765}
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for section, values in saved.items():
                if isinstance(values, dict) and section in cfg:
                    cfg[section].update(values)
                else:
                    cfg[section] = values
        except Exception as e:
            print("配置文件读取失败，使用默认配置:", e)
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("配置保存失败:", e)


CONFIG = load_config()
os.makedirs(SHOT_DIR, exist_ok=True)

LOW_LATENCY_OPTS = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;0"
PLAIN_OPTS = "rtsp_transport;tcp"
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    LOW_LATENCY_OPTS if CONFIG["video"].get("low_latency", True) else PLAIN_OPTS
)

app = Flask(__name__)
try:
    app.json.ensure_ascii = False
except Exception:
    app.config["JSON_AS_ASCII"] = False


# ----------------------------------------------------------------------------
# 复用 HTTP 会话（Digest 只握手一次）
# ----------------------------------------------------------------------------
_session_lock = threading.Lock()
_session = {"s": None, "key": None, "ready": False}


def http_session():
    d = CONFIG["device"]
    key = (d["ip"], int(d["port"]), d["user"], d["password"])
    with _session_lock:
        if _session["s"] is None or _session["key"] != key:
            s = requests.Session()
            s.auth = HTTPDigestAuth(d["user"], d["password"])
            s.mount("http://", requests.adapters.HTTPAdapter(
                pool_connections=4, pool_maxsize=8, max_retries=0))
            _session["s"] = s
            _session["key"] = key
            _session["ready"] = False
        return _session["s"]


def isapi_url(path):
    d = CONFIG["device"]
    return "http://%s:%d/ISAPI%s" % (d["ip"], int(d["port"]), path)


def warmup_session():
    """访问一次 deviceInfo，把 Digest nonce 缓存下来，后续指令只走一次往返"""
    try:
        r = http_session().get(isapi_url("/System/deviceInfo"), timeout=6)
        _session["ready"] = (r.status_code == 200)
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)


# ----------------------------------------------------------------------------
# 云台指令发送线程：界面点一下立即返回，实际发送在后台合并执行
# ----------------------------------------------------------------------------
class PtzSender(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._cond = threading.Condition()
        self._cmd = None
        self._running = True
        self.last_error = ""
        self.count = 0

    def submit(self, pan, tilt, zoom):
        with self._cond:
            self._cmd = (pan, tilt, zoom)
            self._cond.notify()

    def _send(self, pan, tilt, zoom):
        d = CONFIG["device"]
        xml = ("<?xml version='1.0' encoding='UTF-8'?>"
               "<PTZData><pan>%d</pan><tilt>%d</tilt><zoom>%d</zoom></PTZData>" % (pan, tilt, zoom))
        try:
            r = http_session().put(isapi_url("/PTZCtrl/channels/%d/continuous" % int(d["channel"])),
                                   data=xml, headers={"Content-Type": "application/xml"}, timeout=3)
            if r.status_code != 200:
                self.last_error = "HTTP %d" % r.status_code
            else:
                self.last_error = ""
        except Exception as e:
            self.last_error = str(e)

    def run(self):
        while self._running:
            with self._cond:
                while self._cmd is None and self._running:
                    self._cond.wait(0.5)
                cmd, self._cmd = self._cmd, None
            if cmd is None:
                continue
            self._send(*cmd)
            self.count += 1
            time.sleep(0.04)  # 合并连点产生的指令


ptz_sender = PtzSender()
ptz_sender.start()


# ----------------------------------------------------------------------------
# 时间水印：相机自带的 OSD 会被画面翻转带反，所以关掉它，改为自己画（永远正向）
# ----------------------------------------------------------------------------
WEEK_CN = "一二三四五六日"
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
_stamp = {"key": None, "rgba": None}
_fonts = {}


def now_text():
    n = datetime.datetime.now()
    return "%s  星期%s  %02d:%02d:%02d" % (n.strftime("%Y-%m-%d"), WEEK_CN[n.weekday()],
                                           n.hour, n.minute, n.second)


def _font(size):
    if size not in _fonts:
        try:
            _fonts[size] = ImageFont.truetype(FONT_PATH, size)
        except Exception:
            _fonts[size] = ImageFont.load_default()
    return _fonts[size]


def draw_stamp(img):
    """在左下角叠加正向时间。必须在画面翻转之后调用，字就不会跟着反。"""
    if not HAS_PIL or not CONFIG.get("video", {}).get("stamp", True):
        return img
    try:
        h, w = img.shape[:2]
        txt = now_text()
        size = max(14, int(h * 0.036))
        key = (txt, size)
        if _stamp["key"] != key:
            f = _font(size)
            box_w = int(f.getlength(txt)) + size
            im = Image.new("RGBA", (box_w, int(size * 1.75)), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            d.text((size // 4, size // 4), txt, font=f, fill=(255, 255, 255, 255),
                   stroke_width=max(1, size // 14), stroke_fill=(0, 0, 0, 240))
            bbox = im.getbbox()
            if bbox:
                im = im.crop(bbox)
            _stamp["rgba"] = np.array(im)
            _stamp["key"] = key

        st = _stamp["rgba"]
        sh, sw = st.shape[:2]
        x = max(0, (w - sw) // 2)          # 居中，避免铺满裁边切掉年份
        y = h - sh - int(h * 0.026)
        if y < 0 or x + sw > w:
            return img
        roi = img[y:y + sh, x:x + sw]
        a = (st[:, :, 3:4].astype(np.float32) / 255.0)
        roi[:] = (roi.astype(np.float32) * (1 - a) + st[:, :, :3].astype(np.float32) * a).astype(np.uint8)
    except Exception:
        pass
    return img


def camera_osd_off():
    """关掉相机自带的日期时间叠加（否则翻转后会出现一份反的字）"""
    try:
        u = isapi_url("/System/Video/inputs/channels/1/overlays/DateTimeOverlay")
        r = http_session().get(u, timeout=5)
        if r.status_code != 200 or "<enabled>false</enabled>" in r.text:
            return
        xml = ("<OsdDatetime><enabled>false</enabled><positionX>0</positionX><positionY>560</positionY>"
               "<dateStyle>CHR-YYYY-MM-DD</dateStyle><timeStyle>24hour</timeStyle>"
               "<displayWeek>true</displayWeek></OsdDatetime>")
        http_session().put(u, data=xml, headers={"Content-Type": "application/xml"}, timeout=6)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 视频采集线程
# ----------------------------------------------------------------------------
class CameraWorker:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame_full = None
        self.frame_jpg = None
        self.fps = 0.0
        self.width = 0
        self.height = 0
        self.connected = False
        self.error = "未连接"
        self.model = ""
        self.fail_count = 0
        self.max_fail = 5
        self._active = threading.Event()
        self._stop = False
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def start(self, device):
        self.device = device
        self.fail_count = 0
        self._active.set()

    def stop(self):
        self._active.clear()

    def _rtsp_url(self, device):
        user = quote(str(device["user"]), safe="")
        pwd = quote(str(device["password"]), safe="")
        ch = 101 if str(device.get("stream", "sub")) == "main" else 102
        return "rtsp://%s:%s@%s:554/Streaming/Channels/%d" % (user, pwd, device["ip"], ch)

    def _loop(self):
        cap = None
        last_encode = 0.0
        counter, fps_ts = 0, time.time()
        pw = int(CONFIG["video"].get("preview_width", 900))
        interval = 1.0 / max(5, int(CONFIG["video"].get("preview_fps", 16)))

        while not self._stop:
            if not self._active.is_set():
                if cap is not None:
                    cap.release()
                    cap = None
                    with self.lock:
                        self.connected = False
                        self.error = "未连接"
                        self.frame_full = None
                        self.frame_jpg = None
                time.sleep(0.3)
                continue

            if cap is None:
                if self.fail_count >= self.max_fail:
                    with self.lock:
                        self.connected = False
                        self.error = "连接失败，已暂停重试，请检查后点「连接」"
                    time.sleep(0.4)
                    continue
                try:
                    cap = cv2.VideoCapture(self._rtsp_url(self.device), cv2.CAP_FFMPEG)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    if not cap.isOpened():
                        cap.release()
                        cap = None
                        self.fail_count += 1
                        with self.lock:
                            self.connected = False
                            self.error = "打开视频流失败（第 %d 次）：密码错或账号被临时锁定" % self.fail_count
                        time.sleep(min(3 * self.fail_count, 15))
                        continue
                    self.fail_count = 0
                    with self.lock:
                        self.connected = True
                        self.error = ""
                except Exception as e:
                    cap = None
                    self.fail_count += 1
                    with self.lock:
                        self.connected = False
                        self.error = "连接异常：%s" % e
                    time.sleep(min(3 * self.fail_count, 15))
                    continue

            ok, frame = cap.read()
            if not ok or frame is None:
                cap.release()
                cap = None
                self.fail_count += 1
                with self.lock:
                    self.connected = False
                    self.error = "读取画面失败，正在重连（第 %d 次）" % self.fail_count
                time.sleep(min(2 * self.fail_count, 12))
                continue

            flip_mode = CONFIG["video"].get("flip", "none")
            if flip_mode == "rot180":
                frame = cv2.flip(frame, -1)      # 上下 + 左右都翻（等于转 180°）
            elif flip_mode == "mirror":
                frame = cv2.flip(frame, 1)       # 左右镜像
            elif flip_mode == "vflip":
                frame = cv2.flip(frame, 0)       # 上下翻转

            counter += 1
            now = time.time()
            if now - fps_ts >= 1.0:
                self.fps = counter / (now - fps_ts)
                counter, fps_ts = 0, now

            h, w = frame.shape[:2]
            with self.lock:
                self.frame_full = frame
                self.width, self.height = w, h

            if now - last_encode >= interval:
                last_encode = now
                small = frame
                if w > pw:
                    small = cv2.resize(frame, (pw, int(h * pw / w)), interpolation=cv2.INTER_AREA)
                draw_stamp(small)
                ok_jpg, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 74])
                if ok_jpg:
                    with self.lock:
                        self.frame_jpg = buf.tobytes()

        if cap is not None:
            cap.release()


worker = CameraWorker()

if CONFIG["device"].get("password"):
    worker.start(CONFIG["device"])

_PLACEHOLDER = {"jpg": None}


def placeholder_jpg():
    if _PLACEHOLDER["jpg"] is None:
        img = np.zeros((720, 1280, 3), np.uint8)
        img[:] = (18, 21, 27)
        cv2.putText(img, "NO SIGNAL", (470, 345), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (72, 80, 96), 3)
        cv2.putText(img, "check connection and click connect", (432, 400),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (60, 68, 82), 2)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        _PLACEHOLDER["jpg"] = buf.tobytes() if ok else b""
    return _PLACEHOLDER["jpg"]


@app.route("/video")
def video():
    def gen():
        last = None
        last_ph = 0.0
        while True:
            with worker.lock:
                data = worker.frame_jpg
            if data is None:
                now = time.time()
                if now - last_ph >= 1.0:
                    last_ph = now
                    data = placeholder_jpg()
                else:
                    time.sleep(0.05)
                    continue
            if data is last:
                time.sleep(0.01)
                continue
            last = data
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"

    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")


# ----------------------------------------------------------------------------
# 接口
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 转角 / 补光灯 状态读取
# ----------------------------------------------------------------------------
_ptz_cache = {"ts": 0.0, "data": {}}
_light_cache = {"ts": 0.0, "mode": ""}


def _num(tag, text):
    m = re.search(r"<%s>(-?\d+)</%s>" % (tag, tag), text)
    return int(m.group(1)) if m else None


def read_ptz_status(ttl=1.0):
    """方位角/俯仰角（度）、变倍（倍）。ISAPI 原始单位是 0.1 度 / 0.1 倍"""
    now = time.time()
    if now - _ptz_cache["ts"] < ttl and _ptz_cache["data"]:
        return _ptz_cache["data"]
    try:
        r = http_session().get(isapi_url("/PTZCtrl/channels/1/status"), timeout=4)
        if r.status_code == 200:
            az = _num("azimuth", r.text)
            el = _num("elevation", r.text)
            zm = _num("absoluteZoom", r.text)
            data = {
                "azimuth": None if az is None else round(az / 10.0, 1),
                "elevation": None if el is None else round(el / 10.0, 1),
                "zoom": None if zm is None else round(zm / 10.0, 1),
            }
            _ptz_cache["ts"] = now
            _ptz_cache["data"] = data
            return data
    except Exception:
        pass
    return _ptz_cache["data"]


def read_light(ttl=2.0):
    now = time.time()
    if now - _light_cache["ts"] < ttl and _light_cache["mode"]:
        return _light_cache["mode"]
    try:
        r = http_session().get(isapi_url("/Image/channels/1/supplementLight"), timeout=4)
        if r.status_code == 200:
            m = re.search(r"<supplementLightMode>([^<]+)</supplementLightMode>", r.text)
            if m:
                _light_cache["ts"] = now
                _light_cache["mode"] = m.group(1).strip()
                return _light_cache["mode"]
    except Exception:
        pass
    return _light_cache["mode"]


# ----------------------------------------------------------------------------
# 按角度自动到位：这台不支持 absolute 接口，用「连续转动 + 角度反馈」闭环逼近
# ----------------------------------------------------------------------------
def wrap180(x):
    while x > 180:
        x -= 360
    while x < -180:
        x += 360
    return x


def _ramp(err, gain=6.0, lo=5, hi=55):
    v = err * gain
    if abs(v) < lo:
        v = lo if err > 0 else -lo
    return int(max(-hi, min(hi, v)))


class AngleGoto(object):
    """每转一小段就停、读角度、再修正，直到误差落在容差内"""

    def __init__(self):
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._gen = 0
        self.active = False
        self.msg = ""
        self.target = (None, None)

    def start_move(self, az, el):
        self._cancel.set()
        ptz_sender.submit(0, 0, 0)
        self._gen += 1
        gen = self._gen
        cancel = threading.Event()
        self._cancel = cancel
        with self._lock:
            self.active = True
            self.target = (az, el)
            self.msg = "正在转往 方位 %.1f° / 俯仰 %.1f°" % (az, el)
        threading.Thread(target=self._run, args=(az, el, cancel, gen), daemon=True).start()

    def cancel(self):
        self._cancel.set()
        self._gen += 1
        ptz_sender.submit(0, 0, 0)
        with self._lock:
            self.active = False
            self.msg = "已停止"

    def _run(self, az_t, el_t, cancel, gen):
        # 标定：速度 60 时水平约 0.6°/s 每单位、俯仰约 0.42°/s 每单位（取值略小避免冲过头）
        rate_pan, rate_el = 0.52, 0.40
        tol_az, tol_el = 2.5, 2.0
        reached = False
        stuck = 0
        last_err = None
        try:
            deadline = time.time() + 60
            while not cancel.is_set() and time.time() < deadline:
                st = read_ptz_status(ttl=0)
                az_c, el_c = st.get("azimuth"), st.get("elevation")
                if az_c is None or el_c is None:
                    time.sleep(0.2)
                    continue

                # 水平只有 0–350°，不按 360 环绕，直接走直线差值
                d_az = az_t - az_c
                d_el = el_t - el_c
                if abs(d_az) <= tol_az and abs(d_el) <= tol_el:
                    reached = True
                    break

                # 连续两拍几乎没进展，就不再硬顶（多半是已经到了限位）
                cur = (round(abs(d_az), 1), round(abs(d_el), 1))
                if last_err == cur:
                    stuck += 1
                    if stuck >= 2:
                        break
                else:
                    stuck = 0
                    last_err = cur

                # 两轴按「同一段时间」分配速度：谁需要的速度快就按它定时长，
                # 另一轴用较慢的速度在同样时间里走完，做到同时出发、同时收住。
                v_max, v_min = 80.0, 20.0
                t_pan = abs(d_az) / (rate_pan * v_max) if abs(d_az) > tol_az else 0.0
                t_el = abs(d_el) / (rate_el * v_max) if abs(d_el) > tol_el else 0.0
                dur = min(1.2, max(0.2, max(t_pan, t_el)))

                v_pan = min(v_max, max(v_min, abs(d_az) / (rate_pan * dur))) if t_pan else 0.0
                v_el = min(v_max, max(v_min, abs(d_el) / (rate_el * dur))) if t_el else 0.0

                pan = 0 if not v_pan else int(v_pan + 0.5) * (1 if d_az > 0 else -1)
                tilt = 0 if not v_el else int(v_el + 0.5) * (-1 if d_el > 0 else 1)  # tilt 正 = 抬头

                ptz_sender.submit(pan, tilt, 0)
                time.sleep(dur)
                ptz_sender.submit(0, 0, 0)
                time.sleep(0.4)      # 等刹停稳再读
        finally:
            ptz_sender.submit(0, 0, 0)
            if gen != self._gen or cancel.is_set():
                return
            st = read_ptz_status(ttl=0)
            az_c, el_c = st.get("azimuth"), st.get("elevation")
            with self._lock:
                self.active = False
                if reached:
                    self.msg = "已到位（方位 %.1f° / 俯仰 %.1f°）" % (az_c, el_c)
                elif az_c is None:
                    self.msg = "没转到位（读不到角度）"
                else:
                    self.msg = "尽量到位：停在 方位 %.1f° / 俯仰 %.1f°（误差 %.1f° / %.1f°）" % (
                        az_c, el_c, abs(az_t - az_c), abs(el_t - el_c))


angle_goto = AngleGoto()


@app.route("/api/goto", methods=["POST"])
def api_goto():
    body = request.get_json(force=True) or {}
    try:
        az = float(body.get("azimuth"))
        el = float(body.get("elevation"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "角度填数字，例如 180 和 30"})
    az = az % 360
    if az > 350:
        az = 350.0        # 这台水平只能 0–350°
    if not (0 <= el <= 90):
        return jsonify({"ok": False, "msg": "俯仰角要在 0–90° 之间（0 平视，90 垂直朝下）"})
    angle_goto.start_move(az, el)
    return jsonify({"ok": True, "msg": "开始转往 方位 %.1f° / 俯仰 %.1f°" % (az, el)})


@app.route("/api/goto/cancel", methods=["POST"])
def api_goto_cancel():
    angle_goto.cancel()
    return jsonify({"ok": True, "msg": "已停止"})


@app.route("/api/flip", methods=["POST"])
def api_flip():
    body = request.get_json(force=True) or {}
    mode = body.get("mode")
    if mode not in ("none", "rot180", "mirror", "vflip"):
        return jsonify({"ok": False, "msg": "不支持的方向"})
    CONFIG["video"]["flip"] = mode
    save_config(CONFIG)
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/status")
def api_status():
    st = read_ptz_status()
    with worker.lock:
        return jsonify({
            "connected": worker.connected,
            "error": worker.error,
            "fps": round(worker.fps, 1),
            "width": worker.width,
            "height": worker.height,
            "ip": CONFIG["device"]["ip"],
            "stream": CONFIG["device"]["stream"],
            "speed": CONFIG["ptz"]["speed"],
            "model": worker.model,
            "ptz_count": ptz_sender.count,
            "ptz_error": ptz_sender.last_error,
            "azimuth": st.get("azimuth"),
            "elevation": st.get("elevation"),
            "zoom": st.get("zoom"),
            "light_mode": read_light(),
            "flip": CONFIG["video"].get("flip", "none"),
            "goto_active": angle_goto.active,
            "goto_msg": angle_goto.msg,
            "goto_target": list(angle_goto.target),
        })


def isapi_probe(dev):
    url = "http://%s:%d/ISAPI/System/deviceInfo" % (dev["ip"], int(dev["port"]))
    try:
        r = http_session().get(url, timeout=6)
    except Exception as e:
        return 0, "", "连不上设备：%s" % e
    if r.status_code == 200:
        m = re.search(r"<model>([^<]+)</model>", r.text)
        return 200, (m.group(1).strip() if m else ""), "认证通过"
    if r.status_code == 401:
        return 401, "", "认证失败：密码不对，或账号已因连续输错被锁定（海康连错 5 次锁 30 分钟）"
    return r.status_code, "", "设备返回 HTTP %d" % r.status_code


@app.route("/api/connect", methods=["POST"])
def api_connect():
    body = request.get_json(force=True) or {}
    dev = CONFIG["device"]
    for key in ("ip", "user", "password", "stream"):
        if body.get(key):
            dev[key] = body[key]
    dev["port"] = int(body.get("port") or dev.get("port", 80))
    if body.get("speed"):
        CONFIG["ptz"]["speed"] = int(body["speed"])
    save_config(CONFIG)

    code, model, msg = isapi_probe(dev)
    if code != 200:
        return jsonify({"ok": False, "code": code, "msg": msg, "device": dev})

    worker.model = model or worker.model
    worker.start(CONFIG["device"])
    return jsonify({"ok": True, "msg": "认证通过", "model": model, "device": dev})


@app.route("/api/ptz", methods=["POST"])
def api_ptz():
    body = request.get_json(force=True) or {}
    direction = body.get("dir", "stop")
    speed = max(1, min(100, int(body.get("speed") or CONFIG["ptz"]["speed"])))
    pan = tilt = 0
    if direction == "left":
        pan = -speed
    elif direction == "right":
        pan = speed
    elif direction == "up":
        tilt = speed
    elif direction == "down":
        tilt = -speed

    if CONFIG["ptz"].get("invert_pan"):
        pan = -pan
    if CONFIG["ptz"].get("invert_tilt"):
        tilt = -tilt

    angle_goto.cancel()      # 手动操作优先，打断正在执行的跳转
    ptz_sender.submit(pan, tilt, 0)
    return jsonify({"ok": True, "queued": True, "pan": pan, "tilt": tilt})


@app.route("/api/zoom", methods=["POST"])
def api_zoom():
    body = request.get_json(force=True) or {}
    direction = body.get("dir", "stop")
    speed = max(1, min(100, int(body.get("speed") or CONFIG["ptz"]["speed"])))
    zoom = speed if direction == "in" else (-speed if direction == "out" else 0)
    ptz_sender.submit(0, 0, zoom)
    return jsonify({"ok": True, "queued": True})


@app.route("/api/light", methods=["POST"])
def api_light():
    """补光灯：eventIntelligence=智能自动 / colorVuWhiteLight=常亮白光 / irLight=纯红外 / close=关闭"""
    body = request.get_json(force=True) or {}
    mode = body.get("mode")
    brightness = body.get("brightness")
    valid = ("eventIntelligence", "colorVuWhiteLight", "irLight", "close")
    if mode and mode not in valid:
        return jsonify({"ok": False, "msg": "不支持的补光模式"})

    xml = "<SupplementLight>"
    if mode:
        xml += "<supplementLightMode>%s</supplementLightMode>" % mode
    if brightness is not None:
        xml += "<whiteLightBrightness>%d</whiteLightBrightness>" % max(0, min(100, int(brightness)))
    xml += "</SupplementLight>"

    try:
        r = http_session().put(isapi_url("/Image/channels/1/supplementLight"),
                               data=xml, headers={"Content-Type": "application/xml"}, timeout=6)
        ok = r.status_code == 200
        if ok:
            _light_cache["ts"] = 0.0
            if mode:
                _light_cache["mode"] = mode
        return jsonify({"ok": ok, "code": r.status_code, "msg": r.text[:160]})
    except Exception as e:
        return jsonify({"ok": False, "code": 0, "msg": str(e)})


@app.route("/api/preset", methods=["POST"])
def api_preset():
    body = request.get_json(force=True) or {}
    pid = int(body.get("id", 1))
    action = body.get("action", "goto")
    d = CONFIG["device"]
    try:
        if action == "goto":
            r = http_session().put(isapi_url("/PTZCtrl/channels/%d/presets/%d/goto"
                                             % (int(d["channel"]), pid)), timeout=6)
        elif action == "delete":
            r = http_session().delete(isapi_url("/PTZCtrl/channels/%d/presets/%d"
                                                % (int(d["channel"]), pid)), timeout=6)
        else:
            xml = "<PTZPreset><id>%d</id><presetName>P%d</presetName></PTZPreset>" % (pid, pid)
            r = http_session().put(isapi_url("/PTZCtrl/channels/%d/presets/%d"
                                             % (int(d["channel"]), pid)),
                                   data=xml, headers={"Content-Type": "application/xml"}, timeout=6)
        return jsonify({"ok": r.status_code == 200, "code": r.status_code, "msg": r.text[:160]})
    except Exception as e:
        return jsonify({"ok": False, "code": 0, "msg": str(e)})


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    with worker.lock:
        frame = None if worker.frame_full is None else worker.frame_full.copy()
    if frame is None:
        return jsonify({"ok": False, "msg": "当前没有画面，无法拍照"})
    name = time.strftime("snap_%Y%m%d_%H%M%S.jpg")
    path = os.path.join(SHOT_DIR, name)
    draw_stamp(frame)          # 拍照也带上正向时间
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        return jsonify({"ok": False, "msg": "编码失败"})
    buf.tofile(path)
    return jsonify({"ok": True, "name": name, "path": path})


@app.route("/api/shots")
def api_shots():
    files = [f for f in os.listdir(SHOT_DIR) if f.lower().endswith(".jpg")]
    files.sort(reverse=True)
    return jsonify({"files": files[:24]})


@app.route("/shots/<path:name>")
def shots(name):
    return send_from_directory(SHOT_DIR, name)


@app.route("/api/shots/delete", methods=["POST"])
def api_shots_delete():
    body = request.get_json(force=True) or {}
    name = os.path.basename(str(body.get("name") or ""))
    if not name.lower().endswith(".jpg"):
        return jsonify({"ok": False, "msg": "文件名不合法"})
    path = os.path.join(SHOT_DIR, name)
    if not os.path.exists(path):
        return jsonify({"ok": False, "msg": "文件已不存在"})
    try:
        os.remove(path)
        return jsonify({"ok": True, "msg": "已删除 " + name})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/shots/clear", methods=["POST"])
def api_shots_clear():
    n = 0
    for f in os.listdir(SHOT_DIR):
        if f.lower().endswith(".jpg"):
            try:
                os.remove(os.path.join(SHOT_DIR, f))
                n += 1
            except Exception:
                pass
    return jsonify({"ok": True, "msg": "已清空 %d 张" % n, "count": n})


# ----------------------------------------------------------------------------
# 界面
# ----------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>摄像头控制台</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:#0a0d12; --panel:#11151c; --panel2:#161b24; --line:rgba(255,255,255,.07);
  --txt:#e7ecf3; --dim:#8d98a8; --dim2:#5f6a7a;
  --accent:#4c8dff; --accent2:#2f6fe4; --ok:#2ee6a8; --warn:#ffb020; --err:#ff5c5c;
  --r:14px;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{margin:0;background:radial-gradient(1200px 600px at 15% -10%,#131a26 0%,var(--bg) 55%);
  color:var(--txt);font:14px/1.5 "Microsoft YaHei UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
  overflow:hidden}
.app{height:100vh;display:flex;flex-direction:column}

/* 顶栏 */
.top{height:54px;display:flex;align-items:center;gap:12px;padding:0 18px;
  border-bottom:1px solid var(--line);background:rgba(17,21,28,.7);backdrop-filter:blur(10px)}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;letter-spacing:.4px}
.brand .mark{width:22px;height:22px;border-radius:7px;background:linear-gradient(135deg,var(--accent),#7b5bff);
  display:grid;place-items:center;box-shadow:0 4px 14px rgba(76,141,255,.35)}
.brand .mark svg{width:12px;height:12px;fill:#fff}
.pill{display:flex;align-items:center;gap:7px;padding:5px 11px;border-radius:999px;
  background:rgba(255,255,255,.045);border:1px solid var(--line);font-size:12px;color:var(--dim)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:var(--err);box-shadow:0 0 8px currentColor}
.pill.on .dot{background:var(--ok)}
.pill .mono{font-family:Consolas,monospace;color:#a9b6c6}
.top .spacer{flex:1}
.iconbtn{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;cursor:pointer;
  background:rgba(255,255,255,.05);border:1px solid var(--line);color:var(--dim);transition:.15s}
.iconbtn:hover{background:rgba(255,255,255,.1);color:var(--txt)}

/* 主体 */
.main{flex:1;display:flex;gap:14px;padding:14px;min-height:0}
.viewer{flex:1;position:relative;background:#05070a;border:1px solid var(--line);border-radius:var(--r);
  overflow:hidden;box-shadow:0 20px 50px rgba(0,0,0,.45);min-width:0}
.viewer img{width:100%;height:100%;object-fit:cover;display:block}
.viewer.fit img{object-fit:contain}
.vtop{position:absolute;left:12px;top:12px;display:flex;gap:8px;align-items:center;
  background:rgba(6,9,13,.62);backdrop-filter:blur(8px);border:1px solid var(--line);
  padding:6px 11px;border-radius:999px;font-size:12px;color:#c7d2e0}
.vtop .dot{width:7px;height:7px;border-radius:50%;background:var(--err);box-shadow:0 0 8px currentColor}
.vtop.on .dot{background:var(--ok)}
.vbot{position:absolute;right:12px;bottom:12px;display:flex;gap:8px}
.chip{display:flex;align-items:center;gap:6px;padding:8px 13px;border-radius:10px;cursor:pointer;
  background:rgba(10,13,18,.72);backdrop-filter:blur(8px);border:1px solid var(--line);
  font-size:12.5px;color:#d8e1ee;transition:.15s}
.chip:hover{background:rgba(76,141,255,.22);border-color:rgba(76,141,255,.5)}
.chip svg{width:15px;height:15px;fill:currentColor;opacity:.9}
.flash{position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none;transition:opacity .35s}

/* 右侧 */
.side{width:344px;display:flex;flex-direction:column;gap:10px;overflow-y:auto;padding-right:2px}
.side::-webkit-scrollbar{width:6px}
.side::-webkit-scrollbar-thumb{background:#242c38;border-radius:3px}
.card{background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,0));
  border:1px solid var(--line);border-radius:var(--r);padding:12px 13px 12px}
.card h3{margin:0 0 9px;font-size:11.5px;letter-spacing:.12em;color:var(--dim2);font-weight:600;
  display:flex;align-items:center;gap:6px;text-transform:uppercase}
.card h3::before{content:"";width:3px;height:11px;border-radius:2px;background:var(--accent);opacity:.85}

label{display:block;font-size:11.5px;color:var(--dim2);margin:0 0 5px}
input,select{width:100%;background:#0b0f15;border:1px solid var(--line);color:var(--txt);
  border-radius:9px;padding:8px 10px;font-size:13px;outline:none;transition:.15s}
input:focus,select:focus{border-color:rgba(76,141,255,.55);box-shadow:0 0 0 3px rgba(76,141,255,.12)}
.grid2{display:grid;grid-template-columns:1fr 84px;gap:8px}
.grid2b{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.f{margin-bottom:7px}

.btn{cursor:pointer;border:1px solid var(--line);background:var(--panel2);color:var(--txt);
  border-radius:10px;padding:9px 12px;font-size:13px;transition:.15s;font-family:inherit}
.btn:hover{background:#1d242f;border-color:rgba(255,255,255,.14)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:linear-gradient(180deg,var(--accent),var(--accent2));border-color:transparent;
  box-shadow:0 8px 22px rgba(47,111,228,.32);font-weight:600}
.btn.primary:hover{filter:brightness(1.06)}
.btn.ghost{background:transparent}
.btn.wide{width:100%}

/* 方向盘 */
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:2px auto 10px;max-width:216px;width:100%}
.pad .cell{aspect-ratio:1.0;display:flex}
.pad .btn{flex:1;display:grid;place-items:center;padding:0;border-radius:13px;
  background:linear-gradient(180deg,#1b222c,#141a23);width:100%;height:100%}
.pad .btn svg{width:26px;height:26px;fill:#b3bfd0;transition:.15s}
.pad .btn:hover{background:linear-gradient(180deg,#233043,#18202b);border-color:rgba(76,141,255,.35)}
.pad .btn:hover svg{fill:#fff}
.pad .btn:active{background:rgba(76,141,255,.28);transform:scale(.97)}
.pad .stop svg{width:16px;height:16px;fill:var(--warn)}
.pad .stop:hover{background:rgba(255,176,32,.16);border-color:rgba(255,176,32,.45)}
.pad .blank{visibility:hidden}

.spd{display:flex;align-items:center;gap:10px;margin:2px 0 10px}
.spd label{white-space:nowrap;margin:0}
.spd input[type=range]{-webkit-appearance:none;height:4px;border-radius:3px;padding:0;flex:1;
  background:linear-gradient(90deg,var(--accent) 0%,var(--accent) var(--p,40%),#1c232e var(--p,40%));
  border:none;box-shadow:none}
.spd input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:15px;height:15px;border-radius:50%;
  background:#fff;box-shadow:0 0 0 4px rgba(76,141,255,.18);cursor:pointer}
.spd .val{font-family:Consolas,monospace;font-size:12px;color:#9fb0c6;min-width:30px;text-align:right}

.zoomrow{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}

/* 补光灯 */
.lightrow{display:flex;align-items:center;gap:8px;margin-top:11px}
.seg{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;flex:1}
.seg button{padding:6px 0;font-size:11.5px;border-radius:8px;background:#161c25;
  border:1px solid var(--line);color:var(--dim);cursor:pointer;font-family:inherit;transition:.15s}
.seg button:hover{color:var(--txt);border-color:rgba(255,255,255,.16)}
.seg button.on{background:rgba(76,141,255,.2);border-color:rgba(76,141,255,.55);color:#fff}
.bri{display:flex;align-items:center;gap:9px;margin-top:9px}
.bri input[type=range]{-webkit-appearance:none;height:4px;border-radius:3px;padding:0;flex:1;
  background:linear-gradient(90deg,var(--accent) 0%,var(--accent) var(--p,100%),#1c232e var(--p,100%));
  border:none;box-shadow:none}
.hint{font-size:11px;color:var(--dim2);line-height:1.7;margin-top:10px}

/* 角度跳转 */
.gotorow{display:grid;grid-template-columns:34px 1fr 1fr 50px 44px;gap:6px;align-items:center;margin-top:11px}
.gotorow label{margin:0;white-space:nowrap}
.gotorow input{padding:7px 6px;font-size:12.5px;text-align:center}
.gotorow .btn{padding:7px 0;font-size:12px}

/* 缩略图 */
.thumbs{display:grid;grid-template-columns:repeat(3,1fr);gap:7px}
.thumbs img{width:100%;aspect-ratio:16/10;object-fit:cover;border-radius:8px;border:1px solid var(--line);
  cursor:pointer;transition:.15s}
.thumbs img:hover{border-color:rgba(76,141,255,.5);transform:translateY(-1px)}
.th{position:relative}
.th .x{position:absolute;top:3px;right:3px;width:17px;height:17px;border-radius:50%;line-height:1;
  background:rgba(8,11,16,.8);border:1px solid var(--line);color:#cfd8e4;font-size:11px;
  display:grid;place-items:center;cursor:pointer;opacity:0;transition:.15s;font-style:normal}
.th:hover .x{opacity:1}
.th .x:hover{background:var(--err);color:#fff;border-color:var(--err)}
.mini{font-size:11px;color:var(--dim);cursor:pointer;margin-left:auto;letter-spacing:0;text-transform:none;
  font-weight:400;transition:.15s}
.mini:hover{color:var(--err)}
.empty{font-size:11.5px;color:var(--dim2)}

.toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,10px);background:#151b24;
  border:1px solid var(--line);box-shadow:0 14px 40px rgba(0,0,0,.5);padding:10px 18px;border-radius:11px;
  font-size:13px;opacity:0;transition:.22s;pointer-events:none;max-width:80vw}
.toast.on{opacity:1;transform:translate(-50%,0)}
</style>
</head>
<body>
<div class="app">
  <div class="top">
    <div class="brand">
      <span class="mark"><svg viewBox="0 0 24 24"><path d="M4 6h11a3 3 0 013 3v6a3 3 0 01-3 3H4a2 2 0 01-2-2V8a2 2 0 012-2zm14 3l4-2v10l-4-2z"/></svg></span>
      视频巡检控制台
    </div>
    <div class="pill" id="pill"><span class="dot"></span><span id="state">未连接</span></div>
    <div class="pill"><span class="mono" id="res">—</span></div>
    <div class="pill"><span class="mono" id="fps">—</span></div>
    <div class="pill"><span class="mono" id="angle">方位 — · 俯仰 —</span></div>
    <div class="pill"><span class="mono" id="zoomv">倍率 —</span></div>
    <div class="spacer"></div>
    <div class="pill" title="设备型号"><span class="mono" id="model">{{ model }}</span></div>
  </div>

  <div class="main">
    <div class="viewer" id="viewer">
      <img id="view" src="/video" alt="视频">
      <div class="vtop" id="vt"><span class="dot"></span><span id="vtext">等待视频流…</span></div>
      <div class="vbot">
        <div class="chip" onclick="snap()"><svg viewBox="0 0 24 24"><path d="M9 3l-1.5 2H5a2 2 0 00-2 2v11a2 2 0 002 2h14a2 2 0 002-2V7a2 2 0 00-2-2h-2.5L15 3H9zm3 5a5 5 0 110 10 5 5 0 010-10zm0 2a3 3 0 100 6 3 3 0 000-6z"/></svg>拍照</div>
        <div class="chip" id="flipbtn" onclick="cycleFlip()"><svg viewBox="0 0 24 24"><path d="M12 4V1L8 5l4 4V6a6 6 0 016 6h2a8 8 0 00-8-8zm-6 8a6 6 0 006 6v3l4-4-4-4v3a8 8 0 01-8-8z"/></svg><span id="fliplabel">方向</span></div>
        <div class="chip" id="fitbtn" onclick="toggleFit()"><svg viewBox="0 0 24 24"><path d="M3 3h7v2H5v5H3V3zm18 0v7h-2V5h-5V3h7zM3 14h2v5h5v2H3v-7zm18 0v7h-7v-2h5v-5h2z"/></svg><span id="fitlabel">铺满</span></div>
        <div class="chip" onclick="toggleFull()"><svg viewBox="0 0 24 24"><path d="M4 9V4h5v2H6v3H4zm16 0h-2V6h-3V4h5v5zM4 15h2v3h3v2H4v-5zm16 0v5h-5v-2h3v-3h2z"/></svg>全屏</div>
      </div>
      <div class="flash" id="flash"></div>
    </div>

    <div class="side">
      <div class="card">
        <h3>设备连接</h3>
        <div class="f grid2">
          <div><label>IP 地址</label><input id="ip" value="{{ ip }}"></div>
          <div><label>端口</label><input id="port" value="80"></div>
        </div>
        <div class="f grid2b">
          <div><label>用户名</label><input id="user" value="admin"></div>
          <div><label>密码</label><input id="pwd" type="password" placeholder="激活密码"></div>
        </div>
        <div class="f"><label>码流</label>
          <select id="stream">
            <option value="sub" {{ 'selected' if stream=='sub' else '' }}>子码流（流畅，延迟低）</option>
            <option value="main" {{ 'selected' if stream=='main' else '' }}>主码流（2560×1440，清晰但延迟高）</option>
          </select>
        </div>
        <button class="btn primary wide" onclick="connect()">连接 / 应用</button>
      </div>

      <div class="card">
        <h3>云台控制</h3>
        <div class="pad">
          <div class="cell blank"></div>
          <div class="cell"><button class="btn" data-dir="up"><svg viewBox="0 0 24 24"><path d="M12 6l7 8h-4v4h-6v-4H5z"/></svg></button></div>
          <div class="cell blank"></div>
          <div class="cell"><button class="btn" data-dir="left"><svg viewBox="0 0 24 24"><path d="M6 12l8-7v4h4v6h-4v4z"/></svg></button></div>
          <div class="cell"><button class="btn stop" data-dir="stop"><svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></svg></button></div>
          <div class="cell"><button class="btn" data-dir="right"><svg viewBox="0 0 24 24"><path d="M18 12l-8 7v-4H6v-6h4v-4z"/></svg></button></div>
          <div class="cell blank"></div>
          <div class="cell"><button class="btn" data-dir="down"><svg viewBox="0 0 24 24"><path d="M12 18l-7-8h4V6h6v4h4z"/></svg></button></div>
          <div class="cell blank"></div>
        </div>
        <div class="spd">
          <label style="margin:0">转速</label>
          <input id="speed" type="range" min="5" max="100" value="{{ speed }}">
          <span class="val" id="spdval">{{ speed }}</span>
        </div>
        <div class="zoomrow">
          <button class="btn" data-zoom="out">缩小</button>
          <button class="btn" data-zoom="stop">停止</button>
          <button class="btn" data-zoom="in">放大</button>
        </div>
        <div class="lightrow">
          <label style="margin:0;white-space:nowrap">补光</label>
          <div class="seg" id="seglight">
            <button data-mode="eventIntelligence" class="on">智能</button>
            <button data-mode="colorVuWhiteLight">白光</button>
            <button data-mode="irLight">红外</button>
            <button data-mode="close">关</button>
          </div>
        </div>
        <div class="bri" id="brirow" style="display:none">
          <label style="margin:0;white-space:nowrap">亮度</label>
          <input id="bri" type="range" min="0" max="100" value="100">
          <span class="val" id="brival">100</span>
        </div>
        <div class="gotorow">
          <label style="margin:0">跳转</label>
          <input id="gaz" type="number" min="0" max="360" step="0.1" placeholder="方位°">
          <input id="gel" type="number" min="0" max="90" step="0.1" placeholder="俯仰°">
          <button class="btn primary" onclick="gotoAngle()">转到</button>
          <button class="btn" onclick="gotoCancel()">停</button>
        </div>
        <div class="hint" id="gmsg">按住转动松手即停；也可输入角度自动到位。方向键同样可用。</div>
      </div>

      <div class="card">
        <h3>拍照与预置点</h3>
        <button class="btn primary wide" onclick="snap()">拍照保存</button>
        <div class="f" style="margin-top:10px"><label>预置点编号（1–300）</label>
          <div class="grid2b" style="grid-template-columns:1fr 52px 52px 52px">
            <input id="pid" value="1">
            <button class="btn" onclick="preset('goto')">调用</button>
            <button class="btn" onclick="preset('set')">存为</button>
            <button class="btn" onclick="preset('delete')">删除</button>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>最近拍照<span class="mini" onclick="clearShots()">清空</span></h3>
        <div class="thumbs" id="thumbs"><span class="empty">暂无</span></div>
      </div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let holdTimer=null, holding=false, gotoRunning=false;
const $=id=>document.getElementById(id);

function toast(m){const t=$('toast');t.textContent=m;t.classList.add('on');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('on'),2000);}
const speed=()=>parseInt($('speed').value,10);

$('speed').addEventListener('input',e=>{
  $('spdval').textContent=e.target.value;
  e.target.style.setProperty('--p', e.target.value+'%');
});
$('speed').style.setProperty('--p', $('speed').value+'%');

async function post(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body||{})});
  return r.json();
}

async function connect(){
  const body={
    ip:$('ip').value.trim(), port:parseInt($('port').value,10)||80,
    user:$('user').value.trim(), password:$('pwd').value, stream:$('stream').value,
    speed:speed()
  };
  const r=await post('/api/connect',body);
  if(!r.ok){ toast(r.msg||'连接失败'); $('vtext').textContent=r.msg||'连接失败'; return; }
  toast('认证通过'+(r.model?'：'+r.model:'')+'，正在拉流…');
  $('model').textContent=r.model||$('model').textContent;
  $('view').src='/video?t='+Date.now();
}

/* 云台：按下立即发，按住每 250ms 续发，松手立刻发 stop */
function ptz(dir){ post('/api/ptz',{dir:dir,speed:speed()}); }
function startHold(dir){
  if(gotoRunning){ gotoRunning=false; post('/api/goto/cancel',{}); }
  holding=true; ptz(dir);
  clearInterval(holdTimer);
  holdTimer=setInterval(()=>{ if(holding) ptz(dir); },250);
}
function stopHold(){
  if(!holding) return;
  holding=false; clearInterval(holdTimer); ptz('stop');
}
document.querySelectorAll('[data-dir]').forEach(btn=>{
  const dir=btn.dataset.dir;
  if(dir==='stop'){ btn.addEventListener('click',()=>{holding=false;clearInterval(holdTimer);ptz('stop');}); return; }
  btn.addEventListener('pointerdown',e=>{e.preventDefault();btn.setPointerCapture(e.pointerId);startHold(dir);});
  btn.addEventListener('pointerup',stopHold);
  btn.addEventListener('pointercancel',stopHold);
  btn.addEventListener('contextmenu',e=>e.preventDefault());
});
window.addEventListener('blur',stopHold);
document.addEventListener('pointerup',stopHold);

document.querySelectorAll('[data-zoom]').forEach(btn=>{
  const dir=btn.dataset.zoom;
  if(dir==='stop'){ btn.addEventListener('click',()=>post('/api/zoom',{dir:'stop'})); return; }
  btn.addEventListener('pointerdown',e=>{e.preventDefault();btn.setPointerCapture(e.pointerId);
    post('/api/zoom',{dir:dir,speed:speed()});});
  const stop=()=>post('/api/zoom',{dir:'stop'});
  btn.addEventListener('pointerup',stop);
  btn.addEventListener('pointerleave',stop);
});

const KEYMAP={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right'};
document.addEventListener('keydown',e=>{
  if(KEYMAP[e.key] && !e.repeat && !e.target.matches('input,select,textarea')){
    e.preventDefault(); startHold(KEYMAP[e.key]);
  }
});
document.addEventListener('keyup',e=>{
  if(KEYMAP[e.key] && !e.target.matches('input,select,textarea')){ e.preventDefault(); stopHold(); }
});

async function snap(){
  const r=await post('/api/snapshot',{});
  if(r.ok){
    const f=$('flash'); f.style.transition='none'; f.style.opacity='.55';
    requestAnimationFrame(()=>{ f.style.transition='opacity .35s'; f.style.opacity='0'; });
    toast('已保存 '+r.name); loadShots();
  } else toast(r.msg||'拍照失败');
}

async function preset(action){
  const id=parseInt($('pid').value,10)||1;
  const r=await post('/api/preset',{id:id,action:action});
  const okTxt={goto:'已调用预置点 '+id,set:'已保存为预置点 '+id,delete:'已删除预置点 '+id}[action]||'操作成功';
  toast(r.ok?okTxt:'操作失败（'+(r.msg||r.code||'接口不支持')+'）');
}

async function loadShots(){
  const r=await fetch('/api/shots').then(x=>x.json());
  const box=$('thumbs');
  if(!r.files.length){ box.innerHTML='<span class="empty">暂无</span>'; return; }
  box.innerHTML=r.files.slice(0,9).map(n=>
    `<div class="th"><img src="/shots/${n}" title="${n}" onclick="window.open('/shots/${n}')">`+
    `<i class="x" title="删除这张" onclick="delShot(event,'${n}')">×</i></div>`).join('');
}

async function delShot(ev,name){
  ev.stopPropagation();
  const r=await post('/api/shots/delete',{name:name});
  toast(r.ok?'已删除该照片':(r.msg||'删除失败'));
  loadShots();
}

async function clearShots(){
  if(!confirm('确定清空 captures 目录里所有照片？')) return;
  const r=await post('/api/shots/clear',{});
  toast(r.ok?r.msg:'清空失败');
  loadShots();
}

/* 按角度自动到位 */
async function gotoAngle(){
  const az=parseFloat($('gaz').value), el=parseFloat($('gel').value);
  if(isNaN(az)||isNaN(el)){ toast('方位和俯仰都要填数字'); return; }
  const r=await post('/api/goto',{azimuth:az,elevation:el});
  toast(r.ok?r.msg:(r.msg||'转动失败'));
}
async function gotoCancel(){
  const r=await post('/api/goto/cancel',{});
  toast(r.msg||'已停止');
}

/* 画面方向：正常 / 翻转180° / 左右反 / 上下反 */
const FLIPS=[['none','方向'],['rot180','翻转180°'],['mirror','左右反'],['vflip','上下反']];
let flipIdx=0;
function setFlipUI(m){
  const i=FLIPS.findIndex(f=>f[0]===m);
  flipIdx = i<0 ? 0 : i;
  $('fliplabel').textContent=FLIPS[flipIdx][1];
}
async function cycleFlip(){
  const next=FLIPS[(flipIdx+1)%FLIPS.length];
  const r=await post('/api/flip',{mode:next[0]});
  if(r.ok){ setFlipUI(next[0]); toast('画面方向：'+next[1]); }
  else toast('切换失败');
}

function toggleFull(){
  const v=$('viewer');
  if(!document.fullscreenElement){ v.requestFullscreen&&v.requestFullscreen(); }
  else document.exitFullscreen();
}

/* 铺满 / 适应：铺满会裁掉画面边缘，适应保留完整画面 */
function toggleFit(){
  const v=$('viewer');
  v.classList.toggle('fit');
  $('fitlabel').textContent = v.classList.contains('fit') ? '适应' : '铺满';
}

/* 补光灯 */
const LIGHT_NAME={eventIntelligence:'智能（夜间红外，来人切白光）',colorVuWhiteLight:'常亮白光',
  irLight:'仅红外',close:'补光关闭'};
function setLightUI(m){
  document.querySelectorAll('#seglight button').forEach(b=>b.classList.toggle('on', b.dataset.mode===m));
  $('brirow').style.display = (m==='colorVuWhiteLight') ? 'flex' : 'none';
}
document.querySelectorAll('#seglight button').forEach(btn=>{
  btn.addEventListener('click', async ()=>{
    const r=await post('/api/light',{mode:btn.dataset.mode});
    if(r.ok){ setLightUI(btn.dataset.mode); toast(LIGHT_NAME[btn.dataset.mode]||'已切换'); }
    else toast('设置失败：'+(r.msg||''));
  });
});
$('bri').addEventListener('input',e=>{
  $('brival').textContent=e.target.value;
  e.target.style.setProperty('--p', e.target.value+'%');
});
$('bri').addEventListener('change',async e=>{
  const r=await post('/api/light',{mode:'colorVuWhiteLight',brightness:parseInt(e.target.value,10)});
  toast(r.ok?'白光亮度已设为 '+e.target.value:'亮度设置失败');
});
$('bri').style.setProperty('--p','100%');

async function poll(){
  try{
    const s=await fetch('/api/status').then(x=>x.json());
    $('pill').classList.toggle('on',s.connected);
    $('vt').classList.toggle('on',s.connected);
    $('state').textContent=s.connected?'在线':(s.error&&s.error!=='未连接'?s.error:'未连接');
    $('res').textContent=s.width?s.width+'×'+s.height:'—';
    $('fps').textContent=s.fps?s.fps+' fps':'—';
    if(s.model) $('model').textContent=s.model;
    $('angle').textContent='方位 '+(s.azimuth==null?'—':s.azimuth+'°')+' · 俯仰 '+(s.elevation==null?'—':s.elevation+'°');
    $('zoomv').textContent = s.zoom==null ? '倍率 —' : s.zoom+'×';
    if(s.light_mode) setLightUI(s.light_mode);
    if(s.flip) setFlipUI(s.flip);
    if(s.goto_msg) $('gmsg').textContent=s.goto_msg;
    gotoRunning = !!s.goto_active;
    $('gaz').placeholder = s.azimuth==null ? '方位°' : '当前 '+s.azimuth;
    $('gel').placeholder = s.elevation==null ? '俯仰°' : '当前 '+s.elevation;
    $('vtext').textContent=s.connected
      ? (s.stream==='main'?'主码流':'子码流')+' · '+s.fps+' fps'
      : (s.error&&s.error!=='未连接'?s.error:'等待视频流…');
  }catch(e){}
}

loadShots(); poll(); setInterval(poll,1500);
$('view').addEventListener('error',()=>{ setTimeout(()=>{$('view').src='/video?t='+Date.now();},1500); });
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(
        PAGE,
        ip=CONFIG["device"]["ip"],
        model=worker.model or "DS-2DC2404MW-DE3",
        speed=CONFIG["ptz"]["speed"],
        stream=CONFIG["device"].get("stream", "sub"),
    )


def open_browser(url):
    time.sleep(1.2)
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    host = CONFIG["server"].get("host", "127.0.0.1")
    port = int(CONFIG["server"].get("port", 8765))
    url = "http://%s:%d" % ("127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host, port)

    def boot_probe():
        """启动时读一次型号，并把 Digest nonce 预热好；顺手关掉相机自带的时间叠加"""
        code, model, _msg = isapi_probe(CONFIG["device"])
        if code == 200 and model:
            worker.model = model
        if code == 200:
            camera_osd_off()

    if CONFIG["device"].get("password"):
        threading.Thread(target=boot_probe, daemon=True).start()

    print("=" * 52)
    print(" 摄像头控制台已启动：%s" % url)
    print(" 设备地址：%s" % CONFIG["device"]["ip"])
    print(" 拍照目录：%s" % SHOT_DIR)
    print(" 关闭窗口或按 Ctrl+C 退出")
    print("=" * 52)
    if os.environ.get("NO_BROWSER") != "1":
        threading.Thread(target=open_browser, args=(url,), daemon=True).start()
    app.run(host=host, port=port, threaded=True, use_reloader=False)
