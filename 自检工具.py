# -*- coding: utf-8 -*-
r"""
设备自检工具
用法：.venv\Scripts\python.exe 自检工具.py [密码]
做三件事：1) SADP 发现设备  2) HTTP 认证校验  3) RTSP 取流测试
注意：每次运行会消耗一次登录机会，海康连错 5 次锁 30 分钟，别反复跑。
"""
import socket
import sys
import time
import uuid
import os

import cv2
import requests
from requests.auth import HTTPDigestAuth

IP = "192.168.1.64"
USER = "admin"
PWD = sys.argv[1] if len(sys.argv) > 1 else ""
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


def sadp_discover(timeout=4.0):
    probe = ("<?xml version='1.0' encoding='utf-8'?>"
             "<Probe><Uuid>%s</Uuid><Types>inquiry</Types><ClientType>1</ClientType></Probe>"
             % str(uuid.uuid4())).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    try:
        s.bind(("0.0.0.0", 37020))
    except OSError:
        pass
    for dest in [("239.255.255.250", 37020), (IP, 37020)]:
        for _ in range(2):
            try:
                s.sendto(probe, dest)
            except OSError:
                pass
            time.sleep(0.1)
    out = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            break
        except OSError:
            continue
        txt = data.decode("utf-8", "ignore")
        if "<ProbeMatch>" in txt and addr[0] != "0.0.0.0":
            out.append((addr[0], txt))
    s.close()
    return out


def pick(xml, tag):
    a = xml.find("<%s>" % tag)
    b = xml.find("</%s>" % tag)
    return xml[a + len(tag) + 2:b] if a >= 0 and b > a else ""


print("=" * 56)
print("1) SADP 发现")
found = sadp_discover()
if not found:
    print("   没收到响应：确认相机已上电、网线通、电脑和它同网段")
else:
    for ip, xml in found:
        print("   IP        :", pick(xml, "IPv4Address") or ip)
        print("   型号      :", pick(xml, "DeviceDescription"))
        print("   序列号    :", pick(xml, "DeviceSN"))
        print("   MAC       :", pick(xml, "MAC"))

print("\n2) HTTP 认证校验（密码：%s）" % ("已提供" if PWD else "未提供"))
if not PWD:
    print("   跳过：命令行没带密码")
else:
    try:
        r = requests.get("http://%s/ISAPI/System/deviceInfo" % IP,
                         auth=HTTPDigestAuth(USER, PWD), timeout=8)
        if r.status_code == 200:
            print("   ✅ 认证通过，ISAPI 可用")
        elif r.status_code == 401:
            print("   ❌ 认证失败（401）：密码不对，或账号已因连续输错被锁定（锁 30 分钟）")
        else:
            print("   ⚠ HTTP %d" % r.status_code)
    except Exception as e:
        print("   ❌ 连接失败：%s" % e)

print("\n3) RTSP 取流测试")
if PWD:
    url = "rtsp://%s:%s@%s:554/Streaming/Channels/102" % (USER, PWD, IP)
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        ok, frame = cap.read()
        if ok and frame is not None:
            print("   ✅ 取流成功，画面尺寸 %dx%d" % (frame.shape[1], frame.shape[0]))
        else:
            print("   ⚠ 已连上但取不到帧")
    else:
        print("   ❌ 取流失败：认证被拒或码流不可用")
    cap.release()
else:
    print("   跳过：命令行没带密码")

print("=" * 56)
