# DS-2DC2404MW-DE3 巡检小车云台控制台

给海康威视 **DS-2DC2404MW-DE3** 4 寸球机做的本地网页上机位：一个 Python 单文件后端 + 一个自己排版的网页界面，装在巡检小车的工控机上，用浏览器就能操作摄像头。

不依赖海康 SDK、不装 ActiveX 控件，只用 **RTSP 取图 + ISAPI 发指令**。

```
浏览器 (http://127.0.0.1:8765)
        │  MJPEG 视频 / JSON 指令
        ▼
Flask 单文件后端 ── RTSP 取流（OpenCV）──► DS-2DC2404MW-DE3
                 └─ ISAPI (HTTP Digest) ──┘
```

## 功能

| 模块 | 能做什么 |
|---|---|
| 实时视频 | 主/子码流切换，低延迟参数，可「铺满」或「适应」 |
| 云台方向 | 上/下/左/右，按住持续转、松开即停，八向连贯 |
| 转速 | 1–100 可调（实测 40 约 27°/s，60 约 37°/s） |
| 变倍 | 放大 / 缩小 / 停止 |
| 补光灯 | 智能（自动）· 白光常亮 · 纯红外 · 关闭，白光亮度 0–100 |
| 预置点 | 设点 / 调用 / 删除，最多 300 个 |
| 角度跳转 | 填方位+俯仰，闭环自动到位，**两轴同时转、同时停** |
| 画面方向 | 正常 / 翻转180° / 左右反 / 上下反，记忆到配置 |
| 时间水印 | 程序自绘正向「年月日 星期 时分秒」，翻转也不倒，照片同样带 |
| 拍照 | 全分辨率存 JPG，界面内缩略图浏览、单张删除、清空 |
| 状态回显 | 实时方位角 / 俯仰角 / 倍率 / 补光模式 |

## 硬件与环境

- 摄像头：海康 DS-2DC2404MW-DE3（1/2.7" CMOS，2.8–12 mm 4× 光学，红外 30 m / 白光 10 m，水平 350°、垂直 0–90°，≤10 W，DC12V）
- 上位机：Windows（已在 Windows 10/11 + Python 3.13 验证）
- 网络：电脑与摄像头同一网段，能 ping 通、能访问 `http://<摄像头IP>`

## 快速开始

```bat
git clone <本仓库地址>
cd ds-2dc2404mw-de3-ptz-console

:: 1. 复制配置模板，填上你自己的摄像头 IP 和密码
copy config.example.json config.json

:: 2. 建虚拟环境并装依赖（只需一次）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

:: 3. 启动，浏览器自动打开
.venv\Scripts\python.exe camera_console.py
```

也可以直接双击 `启动摄像头界面.bat`，它会自动建环境、装依赖、开界面。

界面右上角「设备连接」里把 IP / 端口 / 用户名 / 密码填对，点「连接 / 应用」即可，不用改文件。

## 界面速览

- **顶栏**：在线状态、分辨率、帧率、方位 / 俯仰 / 倍率、设备型号
- **左侧视频**：右下角浮层是「拍照」「方向」「铺满/适应」「全屏」
- **右侧云台卡片**：方向盘 + 转速滑条 + 变倍 + 补光 + 角度跳转
- **右侧拍照卡片**：拍照保存、缩略图（悬停出现 × 可删）、清空

「方向」按钮每点一次换一档：正常 → 翻转180° → 左右反 → 上下反，会自动记住。

## 配置文件

`config.json`（不入库，自己按 `config.example.json` 建）：

| 键 | 说明 |
|---|---|
| `device.ip` / `port` | 摄像头地址与 HTTP 端口（默认 80） |
| `device.user` / `password` | 登录账号，出厂常见 `admin` + 激活密码 |
| `device.stream` | `main` 主码流清晰、`sub` 子码流流畅 |
| `ptz.speed` | 默认转速 1–100 |
| `ptz.invert_pan` / `invert_tilt` | 方向反了就在这里取反（也可界面里翻转画面） |
| `video.preview_width` / `preview_fps` | 网页端预览尺寸与帧率，越小越省资源 |
| `video.flip` | `none` / `rot180` / `mirror` / `vflip` |
| `video.stamp` | 是否画时间水印 |
| `server.host` / `port` | 本机监听地址与端口 |

## 用的是哪些 ISAPI 接口

```
GET  /ISAPI/System/deviceInfo                      设备型号
PUT  /ISAPI/PTZCtrl/channels/1/continuous          云台连续转动（pan/tilt/zoom，-100~100）
GET  /ISAPI/PTZCtrl/channels/1/status              回读方位角/俯仰角/倍率（单位 0.1°）
PUT  /ISAPI/PTZCtrl/channels/1/presets/{id}        设预置点
PUT  /ISAPI/PTZCtrl/channels/1/presets/{id}/goto   调用预置点
DEL  /ISAPI/PTZCtrl/channels/1/presets/{id}        删预置点
GET/PUT /ISAPI/Image/channels/1/supplementLight    补光灯模式与白光亮度
GET/PUT /ISAPI/System/Video/inputs/channels/1/overlays/DateTimeOverlay   相机自带时间叠加
RTSP rtsp://<user>:<pwd>@<ip>:554/Streaming/Channels/101                     主码流 2560×1440
```

认证方式为 HTTP Digest，账号密码含特殊字符时请先按 URL 编码处理。

## 已知限制（都是实机踩出来的）

- **水平行程 0–350°**，不是 360°。从 350° 回 0° 只能反着走一整圈。
- **不支持绝对定位**（`absolute` 接口返回 403），所以「按角度到位」是闭环逼近：转一小段 → 读数 → 修正，落点有 1–2° 偏差。
- 俯仰低速有明显死区（速度 14 以下基本不动），小幅修正会磨叽，大角度正常。
- **硬件翻转不可用**（`imageFlip` 写入返回 400），画面方向是软件层翻转，只影响本程序。
- 因此**相机自带的时间叠加已关闭**，改由程序自绘正向时间；代价是 iVMS-4200 等第三方客户端里看不到相机原生时间戳。想恢复去相机网页端「图像 → 字符叠加 → 显示时间」。
- 本机为室内球机，官方不建议长期车载振动环境，装小车请做好减震与防水。

## 目录

```
camera_console.py         主程序（Flask + 内嵌网页，单文件）
启动摄像头界面.bat        一键启动（自动建 venv、装依赖）
自检工具.py              排障：SADP 发现 → 认证校验 → RTSP 取流
config.example.json       配置模板（复制成 config.json 再填）
使用说明.md               面向使用者的详细操作说明
requirements.txt          Python 依赖
captures/                 拍照输出目录（不入库）
```

## 排障

| 现象 | 处理 |
|---|---|
| 网页打开但 NO SIGNAL | 先跑 `自检工具.py`，确认 IP、密码、RTSP 端口 554 通不通 |
| 提示 401 | 密码错，或账号未激活（用 SADP / 相机网页端先激活） |
| 端口被占 | 已有旧进程在跑：`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 找到后结束 |
| 云台点了没反应 | 云台有互锁，连点会被合并；确认转速不为 1 |

> 型号资料与手册版权归海康威视所有，本仓库不附带厂商 PDF；界面与代码为自研实现，与海康官方无关联。
