#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
晚点队列 (LaterQueue) — 桌面宠物版

一只沙漏小精灵蹲在你桌面角落（始终置顶、可拖动、不打断你）。
点它，冒出任务气泡：被打断时答应别人"晚点处理"的事都在里面，
按优先级从上往下排；有空时自己去"领"，做完打勾。右键小精灵管添加和退出。

框架：PySide6（Qt）。数据：~/Library/Application Support/LaterQueue/queue.json
"""

import os
import sys
import re
import json
import uuid
import math
import random
import shutil
import glob
import traceback
import subprocess
from datetime import datetime, timedelta

from PySide6.QtCore import (
    Qt, QPoint, QPointF, QSize, QTimer, QRectF, QObject, QThread, Signal)
from PySide6.QtGui import (
    QPixmap, QImage, QAction, QFont, QColor, QGuiApplication, QPainter,
    QBrush, QPen, QPolygonF, QPainterPath)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
    QMenu, QInputDialog, QScrollArea, QGraphicsDropShadowEffect, QSizePolicy,
    QFrame, QDialog, QMessageBox,
)


# =========================== 数据层 ===========================

APP_NAME = "LaterQueue"
DATA_DIR = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
DATA_FILE = os.path.join(DATA_DIR, "queue.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
MENTIONS_FILE = os.path.join(DATA_DIR, "mentions.json")
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")

# 京ME「被@」监控：joyctl 读消息是纯只读，不会清掉京ME 的未读红点。
# @我 检测优先看 joyctl 返回的结构化 @ 字段；没有结构化字段时再用文本兜底。
# 监控群与检测姓名现在都存在 mentions.json，可在「管理监控群」里增删/修改；
# 默认不写入任何个人姓名，首次使用时请在「管理监控群」里设置。
MENTION_NAME = ""                   # @我 检测姓名默认空，避免源码携带个人信息
POLL_INTERVAL_MS = 10 * 60 * 1000  # 轮询间隔默认值：10 分钟（可在菜单自定义）
POLL_INTERVAL_MIN = 1              # 自定义间隔下限（分钟）：太短会频繁调 joyctl
POLL_INTERVAL_MAX = 120            # 自定义间隔上限（分钟）
POLL_FIRST_DELAY_MS = 3000         # 启动后首轮延迟，避免拖慢启动
# joyctl 读群消息本来就慢（十几秒起，赶上消息多/网络慢更久）。
# 内部超时给到 90s，subprocess 外层再多 30s 缓冲，避免正常慢查询被误判超时。
JOYCTL_TIMEOUT_MS = 90 * 1000      # 传给 joyctl 的内部读取超时
POLL_SUBPROCESS_TIMEOUT = 120      # subprocess 外层超时（秒），须 > 内部超时
# 偶发一次超时不报红：连续 N 次失败才在面板显示⚠，成功即清零。
FAIL_ALERT_THRESHOLD = 2
POLL_BACKOFF_MAX_MIN = 60            # 连续失败时退避轮询，最多放慢到 60 分钟
DISMISSED_KEEP = 500               # dismissed_keys 最多保留条数，防无限增长
DONE_KEEP_DAYS = 14                # 已完成记录保留天数：超期在加载时清掉，防 queue.json 无限增长
DONE_SHOW_MAX = 15                 # 「最近完成」区最多列出条数
LAUNCH_AGENT_LABEL = "com.laterqueue.app"
LAUNCH_AGENT_PATH = os.path.expanduser(
    f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist")
ASSET_PET = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "pet.png")
ASSET_PET_BLINK = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "assets", "pet_blink.png")
PET_WIDTH = 130   # 桌面上小精灵显示宽度（px）

# 动画参数
FLOAT_AMP = 6      # 待机上下浮动幅度（px）
HOVER_SCALE = 1.08  # 悬停放大倍数
JUMP_AMP = 14      # 点击跳动幅度（px）
HG_DRAIN_DUR = 1.1  # 悬停后沙子漏下去的时长（秒）
HG_FLIP_DUR = 0.5   # 漏完后沙漏翻转 180° 的时长（秒）


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_items():
    _ensure_dir()
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                return []
    except Exception:
        return []
    return _prune_done(data)


def _prune_done(items):
    """清掉完成超过 DONE_KEEP_DAYS 天的记录，防 queue.json 无限增长。
    只影响 done 项；pending 永远保留。有删减才回写。"""
    cutoff = datetime.now() - timedelta(days=DONE_KEEP_DAYS)
    kept = []
    for it in items:
        if it.get("status") == "done":
            da = it.get("done_at")
            try:
                if da and datetime.fromisoformat(da) < cutoff:
                    continue   # 超期，丢弃
            except ValueError:
                pass           # done_at 格式异常就保守保留
        kept.append(it)
    if len(kept) != len(items):
        save_items(kept)
    return kept


def save_items(items):
    _ensure_dir()
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def new_item(text):
    return {
        "id": uuid.uuid4().hex,
        "text": text.strip(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "pending",
    }


def compact_text(text):
    """把京ME多行消息压成一行；保留全文，只清理多余空白。"""
    return " ".join((text or "").split())


def _extract_mention_values(value):
    """从 joyctl 可能返回的结构化 @ 字段中提取姓名/ERP/userId 候选值。"""
    vals = []
    if value is None:
        return vals
    if isinstance(value, (str, int, float)):
        vals.append(str(value))
        return vals
    if isinstance(value, dict):
        for key in ("name", "displayName", "nick", "nickname", "erp", "userId", "username"):
            if value.get(key):
                vals.append(str(value.get(key)))
        return vals
    if isinstance(value, list):
        for item in value:
            vals.extend(_extract_mention_values(item))
    return vals


def is_mention_of_me(message, mention_name):
    """判断一条京ME消息是否真的 @ 我：结构化字段优先，文本规则兜底。"""
    name = compact_text(mention_name)
    if not name or not isinstance(message, dict):
        return False
    if name.lower() in {"all", "所有人", "全体成员"}:
        return False

    for field in ("mentions", "atUsers", "mentionedUserIds", "mentionUsers", "atList"):
        values = _extract_mention_values(message.get(field))
        if values:
            return any(compact_text(v) == name for v in values)

    content = message.get("content") or ""
    return re.search(rf"@{re.escape(name)}($|[\s\u2005，。！？、:：；;,.!?])", content) is not None


def classify_poll_error(error_text):
    """把 joyctl 错误分成用户可恢复的类型，用于面板提示与退避。"""
    text = compact_text(error_text).lower()
    if any(k in text for k in ("login", "登录", "unauthorized", "认证", "auth", "token", "cookie")):
        return "login_expired"
    if any(k in text for k in ("timeout", "timed out", "超时")):
        return "timeout"
    return "unknown"


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    _ensure_dir()
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


# =========================== 被@队列数据 ===========================

def load_mentions():
    """读取被@候选与轮询游标。结构见 plan：
    {last_since:{gid:ts}, candidates:[{key,group_id,group_name,sender,sent_at,content}],
     dismissed_keys:[...]}"""
    _ensure_dir()
    base = {"last_since": {}, "candidates": [], "dismissed_keys": [],
            "poll_status": {},   # {ok:bool, at:ts, error:str} 最近一次轮询结果
            "monitor_groups": [],           # [{id, name}]；空白开始，不预置
            "mention_name": MENTION_NAME}   # @我 检测姓名；默认沿用常量
    if not os.path.exists(MENTIONS_FILE):
        return base
    try:
        with open(MENTIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return base
        for k, v in base.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return base


def save_mentions(m):
    _ensure_dir()
    tmp = MENTIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MENTIONS_FILE)


def load_reminders():
    """定时提醒列表：[{id, time:"HH:MM", text, enabled, last_fired:"YYYY-MM-DD"}]。
    每日到点触发；last_fired 记录当天已弹过，防止同一分钟内重复弹。"""
    _ensure_dir()
    if not os.path.exists(REMINDERS_FILE):
        return []
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_reminders(rs):
    _ensure_dir()
    tmp = REMINDERS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REMINDERS_FILE)


def new_reminder(time_str, text):
    return {
        "id": uuid.uuid4().hex,
        "time": time_str,        # "HH:MM"，每日该时刻触发
        "text": text.strip(),
        "enabled": True,
        "last_fired": "",        # 最近一次触发的日期 YYYY-MM-DD，防当天重复
    }


def _joyctl_candidates():
    """列出 joyctl 可能所在的路径。打包成 .app 后经 Finder 启动，PATH 被砍到
    只剩 /usr/bin:/bin:/usr/sbin:/sbin，node/npm 的 bin 目录全都不在里面，
    shutil.which 因此找不到。joyctl 是 npm 全局包，可能装在 homebrew、系统 npm、
    或 nvm/fnm/volta 管理的 node 里，这里把这些常见位置都枚举出来。"""
    home = os.path.expanduser("~")
    fixed = [
        "/opt/homebrew/bin/joyctl",        # homebrew (Apple Silicon)
        "/usr/local/bin/joyctl",           # homebrew (Intel) / 系统 npm
        os.path.join(home, ".local/bin/joyctl"),
        os.path.join(home, ".volta/bin/joyctl"),
        os.path.join(home, "Library/pnpm/joyctl"),
        "/opt/homebrew/lib/node_modules/@jd/joyctl-office/dist/main-office.js",
    ]
    # nvm / fnm 每个 node 版本一个 bin 目录，用 glob 展开
    globbed = []
    for pat in [
        os.path.join(home, ".nvm/versions/node/*/bin/joyctl"),
        os.path.join(home, ".fnm/node-versions/*/installation/bin/joyctl"),
        os.path.join(home, "Library/Application Support/fnm/node-versions/*/installation/bin/joyctl"),
        os.path.join(home, "n/bin/joyctl"),
    ]:
        globbed += glob.glob(pat)
    return fixed + sorted(globbed, reverse=True)   # nvm 多版本时优先较新的


def _resolve_joyctl():
    """返回可执行的 joyctl 路径；找不到就兜底返回 'joyctl' 让报错可见。"""
    p = shutil.which("joyctl")
    if p:
        return p
    for c in _joyctl_candidates():
        if os.path.exists(c):
            return c
    return "joyctl"


# 「@我 监控」依赖京东内部 CLI joyctl（读京ME 群消息）。没装时用这个标记
# 让面板显示友好的安装引导，而不是甩一句看不懂的 Errno 2。
JOYCTL_MISSING = "JOYCTL_MISSING"
JOYCTL_INSTALL_CMD = (
    "npm install -g @jd/joyctl-office --registry=http://registry.m.jd.com")


def _joyctl_available():
    """joyctl 是否真的装了。与 _resolve_joyctl 共用候选列表，避免两处逻辑漂移。"""
    if shutil.which("joyctl"):
        return True
    return any(os.path.exists(c) for c in _joyctl_candidates())


def mention_key(group_id, sent_at, sender):
    return f"{group_id}|{sent_at}|{sender}"


def _fmt_done_at(iso):
    """完成时间 ISO 串 → 「MM-DD HH:MM」；解析不了就原样返回。"""
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso or ""


def _normalize_hhmm(s):
    """把用户输入的时间规整成 "HH:MM"（如 9:5 → 09:05）；非法返回 None。
    支持中文冒号和点分隔。"""
    s = s.strip().replace("：", ":").replace(".", ":").replace("点", ":")
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", s)
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if h > 23 or mm > 59:
        return None
    return f"{h:02d}:{mm:02d}"


# =========================== 开机自启 ===========================

def _app_launch_args():
    real = os.path.realpath(sys.argv[0])
    if ".app/Contents/MacOS/" in real:
        return ["/usr/bin/open", real.split(".app/Contents/MacOS/")[0] + ".app"]
    return None


def launch_at_login_enabled():
    return os.path.exists(LAUNCH_AGENT_PATH)


def set_launch_at_login(enable):
    if enable:
        args = _app_launch_args()
        if not args:
            return False
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>{''.join(f'<string>{a}</string>' for a in args)}</array>
  <key>RunAtLoad</key><true/>
</dict></plist>"""
        os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
        with open(LAUNCH_AGENT_PATH, "w") as f:
            f.write(plist)
        subprocess.run(["launchctl", "load", LAUNCH_AGENT_PATH], check=False)
    else:
        if os.path.exists(LAUNCH_AGENT_PATH):
            subprocess.run(["launchctl", "unload", LAUNCH_AGENT_PATH], check=False)
            os.remove(LAUNCH_AGENT_PATH)
    return True


# =========================== 被@轮询（后台线程） ===========================

class MentionPoller(QObject):
    """在 worker 线程里跑 joyctl（单次约 14 秒，绝不能放主线程）。
    主线程负责一切文件写与 UI；poller 只做阻塞的 subprocess，然后把
    结果通过信号抛回主线程。所需的游标/去重集合由主线程在启动前注入。"""

    # 本轮新候选(list[dict]) + 更新后的 last_since(dict gid->ts)
    #  + 本轮各群错误(list[str]，空表示全成功)
    found = Signal(list, dict, list)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.joyctl = _resolve_joyctl()
        self.since_map = {}    # gid -> 上次所见最大 sent_at；主线程注入
        self.known_keys = set()  # 已在候选/已忽略的 key，用于跳过；主线程注入
        self.groups = []       # 要监控的群 id 列表；主线程注入
        self.mention_name = ""   # @我 检测姓名；主线程注入

    def poll(self):
        new_candidates = []
        updated_since = dict(self.since_map)
        errors = []
        # joyctl 没装：不逐群试（否则每个群都甩一句 Errno 2），
        # 直接发一个特殊标记，让面板显示安装引导。
        if not _joyctl_available():
            self.found.emit([], updated_since, [JOYCTL_MISSING])
            self.finished.emit()
            return
        for gid in self.groups:
            first_run = self.since_map.get(gid) is None
            try:
                cands, latest = self._poll_group(gid)
            except Exception as e:
                # 单群失败（超时/登录态失效/解析错）：记下错误、跳过该群，
                # 不拖累其他群。错误随信号回主线程，让面板/菜单能显示。
                msg = f"群 {gid}：{e}"
                sys.stderr.write(f"[mention] poll failed {msg}\n")
                errors.append(msg)
                continue
            # 冷启动防误报：某群第一次轮询（还没有游标）只用来建立基线，
            # 记录当前最新 sent_at 作为游标，本轮不产出任何候选，
            # 否则会把群里的历史 @我 全部当成「新的」弹出来。
            if first_run:
                if latest:
                    updated_since[gid] = latest
                continue
            for c in cands:
                if c["key"] in self.known_keys:
                    continue
                self.known_keys.add(c["key"])   # 防同一轮内跨群重复
                new_candidates.append(c)
            if latest:
                updated_since[gid] = latest
        self.found.emit(new_candidates, updated_since, errors)
        self.finished.emit()

    def _poll_group(self, gid):
        cmd = [self.joyctl, "chat", "read", "group",
               "--group-id", str(gid), "--from", "others", "--json"]
        since = self.since_map.get(gid)
        if since:
            # 增量拉取也必须带 limit：joyctl 默认只返回约 20-30 条，
            # 群消息密集时一个轮询周期内的新消息会超出默认窗口，
            # 靠后的 @我 就被截掉、永远读不到。放大到 200 覆盖 10 分钟高峰。
            cmd += ["--since", since, "--limit", "200"]
        else:
            cmd += ["--limit", "30"]

        env = dict(os.environ)
        env["JOYCTL_CHAT_READ_TIMEOUT_MS"] = str(JOYCTL_TIMEOUT_MS)
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=POLL_SUBPROCESS_TIMEOUT, env=env)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "joyctl 非零退出").strip()[:200])

        # strict=False：京ME 消息里可能夹裸控制字符（\x0b 等），
        # 默认 json.loads 会抛异常导致整轮 @我 全丢，放宽即可。
        data = json.loads(proc.stdout, strict=False)
        messages = data.get("messages", []) if isinstance(data, dict) else []
        cands, latest = [], since
        for m in messages:
            content = (m.get("content") or "")
            sent_at = m.get("sentAt") or ""
            if sent_at and (latest is None or sent_at > latest):
                latest = sent_at
            if not is_mention_of_me(m, self.mention_name):
                continue
            sender = m.get("sender") or "?"
            cands.append({
                "key": mention_key(gid, sent_at, sender),
                "group_id": str(gid),
                "group_name": m.get("receiver") or str(gid),
                "sender": sender,
                "sent_at": sent_at,
                "content": content,
            })
        return cands, latest


# =========================== 统一弹窗 ===========================

# 黑白极简弹窗共用样式：纯白背景、黑灰字、黑色主按钮、白底描边次按钮。
# 与队列气泡（QueueBubble）主题一致。
DIALOG_QSS = """
    QDialog { background:#ffffff; }
    QLabel { color:#1a1a1a; font-size:14px; }
    QLabel#hint { color:#9a9a9a; font-size:13px; }
    QLabel#gname { color:#1a1a1a; }
    QLabel#nameval { color:#1a1a1a; font-weight:700; }
    QLabel#rtime { color:#1a1a1a; font-weight:700; font-size:14px; }
    QLabel#rtext { color:#1a1a1a; }
    QLabel#rtext_off { color:#c0c0c0; }
    QLineEdit, QSpinBox {
        border:1.5px solid #e3e3e3; border-radius:10px; padding:8px 10px;
        font-size:14px; color:#1a1a1a; background:#ffffff;
        selection-background-color:#1a1a1a; selection-color:#ffffff; }
    QLineEdit:focus, QSpinBox:focus { border-color:#1a1a1a; }
    QPushButton { border:none; border-radius:10px; padding:7px 16px;
                  font-size:13px; }
    QPushButton#primary { background:#1a1a1a; color:white; font-weight:600; }
    QPushButton#primary:hover { background:#000000; }
    QPushButton#ghost { background:transparent; color:#8a8a8a;
                        border:1px solid #e3e3e3; }
    QPushButton#ghost:hover { color:#3a3a3a; border-color:#c8c8c8; }
    QPushButton#add { background:#1a1a1a; color:white; font-weight:600; }
    QPushButton#add:hover { background:#000000; }
    QPushButton#rm { color:#e8442e; background:transparent; }
    QPushButton#rm:hover { color:#c23520; }
    QPushButton#name, QPushButton#tg { background:#f0f0f0; color:#3a3a3a; }
    QPushButton#name:hover, QPushButton#tg:hover { background:#e6e6e6; }
"""


def center_on_pet_screen(dialog, pet):
    """把弹窗移到小精灵所在屏幕的中央。多屏时跟随小人所在屏，
    不会跑到主屏或别的屏。须在 dialog 已知尺寸后调用（show/adjustSize 之后）。"""
    scr = None
    try:
        scr = pet.screen() or QGuiApplication.screenAt(
            pet.frameGeometry().center())
    except Exception:
        pass
    scr = scr or QApplication.primaryScreen()
    geo = scr.availableGeometry()
    x = geo.center().x() - dialog.width() // 2
    y = geo.center().y() - dialog.height() // 2
    dialog.move(x, y)


class SimpleInputDialog(QDialog):
    """统一的黑白极简输入弹窗，替代系统 QInputDialog。
    标题 + 提示 + 单行输入框 + 取消/确定。回车=确定，Esc=取消。
    用静态方法 get_text / get_int 调用，签名贴近 QInputDialog。"""

    def __init__(self, pet, title, label, text="", is_int=False,
                 int_min=0, int_max=100):
        super().__init__()
        self._pet = pet
        self._is_int = is_int
        self.setWindowTitle(title)
        self.setMinimumWidth(340)
        self.setStyleSheet(DIALOG_QSS)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(20, 18, 20, 16)
        vbox.setSpacing(12)

        lab = QLabel(label)
        lab.setWordWrap(True)
        vbox.addWidget(lab)

        if is_int:
            from PySide6.QtWidgets import QSpinBox
            self.edit = QSpinBox()
            self.edit.setRange(int_min, int_max)
            self.edit.setValue(int(text) if str(text).isdigit() else int_min)
        else:
            from PySide6.QtWidgets import QLineEdit
            self.edit = QLineEdit()
            self.edit.setText(text)
            self.edit.returnPressed.connect(self.accept)   # 回车即确定
        vbox.addWidget(self.edit)

        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("ghost")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        ok = QPushButton("确定")
        ok.setObjectName("primary")
        ok.setCursor(Qt.PointingHandCursor)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        btns.addWidget(ok)
        bw = QWidget()
        bw.setLayout(btns)
        vbox.addWidget(bw)

        self.adjustSize()
        center_on_pet_screen(self, pet)
        self.edit.setFocus()

    def value(self):
        return self.edit.value() if self._is_int else self.edit.text()

    @staticmethod
    def get_text(pet, title, label, text=""):
        """返回 (text, ok)，贴近 QInputDialog.getText。"""
        dlg = SimpleInputDialog(pet, title, label, text=text)
        ok = dlg.exec() == QDialog.Accepted
        return (dlg.value() if ok else ""), ok

    @staticmethod
    def get_int(pet, title, label, value=0, minv=0, maxv=100):
        """返回 (int, ok)，贴近 QInputDialog.getInt。"""
        dlg = SimpleInputDialog(pet, title, label, text=str(value),
                                is_int=True, int_min=minv, int_max=maxv)
        ok = dlg.exec() == QDialog.Accepted
        return (dlg.value() if ok else value), ok


class QueueBubble(QWidget):
    """挂在小精灵旁边的圆角小面板，展示并操作队列。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        # NoDropShadowWindowHint：关掉 macOS 合成器按窗口 alpha 蒙版生成的系统投影，
        # 否则半透明窗口在圆角卡片外缘会渲染出一圈深色描边（黑框）。卡片本身的
        # 柔和阴影由下方 QGraphicsDropShadowEffect 单独负责，不受影响。
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        try:
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        except Exception:
            pass

        # 圆角卡片容器
        self.card = QWidget(self)
        self.card.setObjectName("card")
        # 黑白极简主题（参考 JoyChat 风格）：纯白卡片、黑灰字、红色作唯一强调色。
        # 强调红 #e8442e 用于公告竖条 / 「有人喊你」圆点 / 置顶星标 / 徽标。
        # 主按钮「收下」「添加」用纯黑填充白字；次按钮走白底灰描边。
        self.card.setStyleSheet("""
            #card { background: #ffffff; border-radius: 20px; }
            QLabel#title { color:#1a1a1a; font-weight:700; }
            QLabel#count { color:#b8b8b8; }
            QLabel.task { color:#1a1a1a; }
            QLabel#mtitle { color:#8a8a8a; font-weight:600; }
            QLabel.mfrom { color:#1a1a1a; font-weight:700; }
            QLabel.mbody { color:#3a3a3a; }
            QLabel#pollbad { color:#e8442e; font-size:12px; }
            QLabel#pollok { color:#c0c0c0; font-size:11px; }
            QPushButton { border:none; background:transparent; font-size:15px;
                          color:#9a9a9a; padding:0; }
            QPushButton:hover { color:#1a1a1a; }
            QPushButton#add { background:#1a1a1a; color:white; border-radius:14px;
                              font-size:13px; font-weight:600; padding:7px 16px; }
            QPushButton#add:hover { background:#000000; }
            QPushButton#accept { background:#1a1a1a; color:white; border-radius:12px;
                                 font-size:13px; font-weight:600; padding:6px 14px; }
            QPushButton#accept:hover { background:#000000; }
            QPushButton#ignore { color:#8a8a8a; font-size:13px; padding:6px 14px;
                                 border:1px solid #e3e3e3; border-radius:12px; }
            QPushButton#ignore:hover { color:#3a3a3a; border-color:#c8c8c8; }
            QPushButton#acceptall { background:#1a1a1a; color:white; border-radius:10px;
                                    font-size:11px; font-weight:600; padding:4px 9px; }
            QPushButton#acceptall:hover { background:#000000; }
            QPushButton#clearmentions { color:#8a8a8a; font-size:11px; font-weight:600;
                                        padding:4px 9px; border:1px solid #e3e3e3;
                                        border-radius:10px; }
            QPushButton#clearmentions:hover { color:#3a3a3a; border-color:#c8c8c8; }
            QFrame#divider { background:#efefef; max-height:1px; min-height:1px;
                             border:none; }
            QPushButton#donehead { color:#b0b0b0; font-size:12px; font-weight:600;
                                   text-align:left; padding:2px 0; }
            QPushButton#donehead:hover { color:#8a8a8a; }
            QLabel.donetask { color:#b0b0b0; font-size:12px; }
            QLabel.donetime { color:#c8c8c8; font-size:11px; }
            QLabel#joyctlcmd { color:#3a3a3a; font-size:11px;
                               font-family:Menlo,Monaco,monospace;
                               background:#f4f4f4; border-radius:6px;
                               padding:5px 7px; }
        """)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setColor(QColor(0, 0, 0, 45))
        shadow.setOffset(0, 8)
        self.card.setGraphicsEffect(shadow)

        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(18, 18, 18, 18)   # 给阴影留边
        self.outer.addWidget(self.card)

        self.card_box = QVBoxLayout(self.card)
        self.card_box.setContentsMargins(16, 14, 16, 16)
        self.card_box.setSpacing(0)

        self.content = QWidget()
        self.content.setObjectName("content")
        self.content.setStyleSheet("#content { background: transparent; }")

        self.vbox = QVBoxLayout(self.content)
        self.vbox.setContentsMargins(0, 0, 0, 0)
        self.vbox.setSpacing(8)

        self.scroll = QScrollArea(self.card)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setStyleSheet("""
            QScrollArea { border:none; background:transparent; }
            QScrollArea > QWidget > QWidget { background:transparent; }
            QScrollBar:vertical { background:transparent; width:6px; margin:2px 0; }
            QScrollBar::handle:vertical { background:#dedede; border-radius:3px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
        """)
        self.scroll.setWidget(self.content)
        self.card_box.addWidget(self.scroll)

        self._show_done = False   # 「最近完成」区默认收起

        self.refresh()

    def refresh(self):
        old_scroll = self.scroll.verticalScrollBar().value()

        # 清空
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        pending = self.app.pending()
        cands = self.app.candidates()

        # 被@候选区（仅在有候选时显示，排在「待办事项」上方）
        if cands:
            mhead = QHBoxLayout()
            mhead.setSpacing(7)
            mdot = QLabel("●")
            mdot.setStyleSheet("color:#e8442e; font-size:10px;")
            mhead.addWidget(mdot)
            mtitle = QLabel("有人喊你")
            mtitle.setObjectName("mtitle")
            mtitle.setFont(QFont("", 13))
            mhead.addWidget(mtitle)
            mhead.addStretch(1)
            accept_all = QPushButton("全部收下")
            accept_all.setObjectName("acceptall")
            accept_all.setCursor(Qt.PointingHandCursor)
            accept_all.setToolTip("把当前所有 @ 候选转为待办事项")
            accept_all.clicked.connect(self.app.accept_all_mentions)
            mhead.addWidget(accept_all)

            clear_all = QPushButton("清空")
            clear_all.setObjectName("clearmentions")
            clear_all.setCursor(Qt.PointingHandCursor)
            clear_all.setToolTip("忽略当前所有 @ 候选，不再拉回")
            clear_all.clicked.connect(self.app.clear_mentions)
            mhead.addWidget(clear_all)

            mcnt = QLabel(f"{len(cands)}")
            mcnt.setObjectName("count")
            mhead.addWidget(mcnt)
            mhw = QWidget()
            mhw.setLayout(mhead)
            self.vbox.addWidget(mhw)

            for c in cands:
                self.vbox.addWidget(self._mention_row(c))

            div = QFrame()
            div.setObjectName("divider")
            self.vbox.addWidget(div)

        # 头部
        head = QHBoxLayout()
        head.setSpacing(7)
        pdot = QLabel("●")
        pdot.setStyleSheet("color:#cfcfcf; font-size:10px;")
        head.addWidget(pdot)
        title = QLabel("待办事项")
        title.setObjectName("title")
        title.setFont(QFont("", 13))
        head.addWidget(title)
        head.addStretch(1)
        cnt = QLabel(f"{len(pending)}" if pending else "已为你清空")
        cnt.setObjectName("count")
        head.addWidget(cnt)
        hw = QWidget()
        hw.setLayout(head)
        self.vbox.addWidget(hw)

        # 任务行
        if not pending:
            empty = QLabel("当前没有待处理事项，我会继续替你留意。")
            empty.setStyleSheet("color:#b8b8b8;")
            self.vbox.addWidget(empty)
        else:
            for it in pending:
                self.vbox.addWidget(self._row(it))

        # 添加按钮
        add = QPushButton("＋ 添加待办")
        add.setObjectName("add")
        add.clicked.connect(self.app.add_via_dialog)
        addw = QHBoxLayout()
        addw.addStretch(1)
        addw.addWidget(add)
        addw.addStretch(1)
        aw = QWidget()
        aw.setLayout(addw)
        self.vbox.addWidget(aw)

        # 「最近完成」折叠区：完成的事没删只是不显示，这里给个回顾入口。
        # 默认收起，点标题展开；只列最近若干条，旧的加载时已自动过期。
        done = self.app.done_recent()
        if done:
            div = QFrame()
            div.setObjectName("divider")
            self.vbox.addWidget(div)

            arrow = "▾" if self._show_done else "▸"
            head = QPushButton(f"{arrow} 最近完成 {len(done)}")
            head.setObjectName("donehead")
            head.setCursor(Qt.PointingHandCursor)
            head.clicked.connect(self._toggle_done)
            self.vbox.addWidget(head)

            if self._show_done:
                for it in done:
                    self.vbox.addWidget(self._done_row(it))

        # 轮询状态行：连续多次失败才醒目提示（打包成 .app 后 stderr 不可见，
        # 全靠这里让用户知道「@我 检查失败了」，比如京ME 登录态失效）。
        # 偶发一次超时不亮红，退回显示淡色「上次检查」，不打扰。
        if self.app.monitor_group_ids():
            st = self.app.poll_status()
            if not self.app.mention_name():
                tip = QLabel("先设置你的 @ 名称，我才能更准确地替你留意消息。")
                tip.setObjectName("pollbad")
                tip.setWordWrap(True)
                tip.setMaximumWidth(300)
                self.vbox.addWidget(tip)
            elif getattr(self.app, "_checking", False):
                # 用户点了「立即检查」：立刻显示，别让人以为没反应（joyctl 约十几秒）
                chk = QLabel("正在检查 @我…")
                chk.setObjectName("pollok")
                self.vbox.addWidget(chk)
            elif st and st.get("joyctl_missing"):
                # 没装 joyctl：给友好的安装引导，而不是甩 Errno 2。
                tip = QLabel("⚠ 需要先装 joyctl 才能监控 @我：")
                tip.setObjectName("pollbad")
                tip.setWordWrap(True)
                tip.setMaximumWidth(300)
                self.vbox.addWidget(tip)

                cmd = QLabel(JOYCTL_INSTALL_CMD)
                cmd.setObjectName("joyctlcmd")
                cmd.setWordWrap(True)
                cmd.setMaximumWidth(300)
                cmd.setTextInteractionFlags(Qt.TextSelectableByMouse)
                cmd.setToolTip("在终端运行这条命令（可选中复制）")
                self.vbox.addWidget(cmd)

                note = QLabel("装完还要在终端登录一次京ME。")
                note.setObjectName("pollok")
                note.setWordWrap(True)
                note.setMaximumWidth(300)
                self.vbox.addWidget(note)
            elif st and st.get("login_expired"):
                tip = QLabel("京 ME 登录状态可能已过期，请重新登录 joyctl 后我再继续为你留意。")
                tip.setObjectName("pollbad")
                tip.setWordWrap(True)
                tip.setMaximumWidth(300)
                self.vbox.addWidget(tip)

                cmd = QLabel("joyctl login")
                cmd.setObjectName("joyctlcmd")
                cmd.setTextInteractionFlags(Qt.TextSelectableByMouse)
                cmd.setToolTip("在终端运行这条命令（可选中复制）")
                self.vbox.addWidget(cmd)
            elif st and st.get("alert"):
                warn = QLabel(f"⚠ 检查失败（{st.get('at', '')}）：{st.get('error', '')}")
                warn.setObjectName("pollbad")
                warn.setWordWrap(True)
                warn.setMaximumWidth(300)
                self.vbox.addWidget(warn)
            elif st and st.get("at"):
                okl = QLabel(f"上次为你检查 {st.get('at')}")
                okl.setObjectName("pollok")
                self.vbox.addWidget(okl)

        self._finish_layout(old_scroll)

    def _finish_layout(self, old_scroll=None):
        """收尾布局：内容刚刷新时 Qt 可能还没算出 sizeHint，0 高度时先不覆盖旧尺寸。"""
        if not self._fit_content_to_screen():
            return
        self.card.adjustSize()
        self.adjustSize()
        if old_scroll is not None:
            bar = self.scroll.verticalScrollBar()
            bar.setValue(min(old_scroll, bar.maximum()))

    def _fit_content_to_screen(self):
        """候选消息可能很多：限制面板高度，避免 Qt 为塞进屏幕压扁卡片。"""
        self.vbox.invalidate()
        self.vbox.activate()
        hint = self.vbox.sizeHint()
        if hint.width() <= 0 or hint.height() <= 0:
            return False
        self.content.setMinimumSize(hint)
        self.content.resize(hint)

        screen = None
        if hasattr(self.app, "_screen_geo"):
            try:
                screen = self.app._screen_geo()
            except Exception:
                screen = None
        if screen is None:
            primary = QApplication.primaryScreen()
            screen = primary.availableGeometry() if primary else None

        available_h = screen.height() if screen else 760
        max_scroll_h = max(220, int(available_h * 0.72))
        scroll_h = min(hint.height(), max_scroll_h)
        self.scroll.setFixedHeight(scroll_h)

        scrollbar_w = self.scroll.verticalScrollBar().sizeHint().width()
        scroll_w = hint.width() + (scrollbar_w if hint.height() > scroll_h else 0)
        self.scroll.setMinimumWidth(scroll_w)
        return True

    def _toggle_done(self):
        self._show_done = not self._show_done
        self.refresh()

    def _done_row(self, it):
        """一条已完成记录：淡色文本 + 完成时间。只读，不带操作按钮。"""
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(4, 0, 0, 0)

        lbl = QLabel(it.get("text", ""))
        lbl.setProperty("class", "donetask")
        lbl.setMinimumWidth(180)
        lbl.setMaximumWidth(220)
        lbl.setWordWrap(False)
        row.addWidget(lbl, 1)

        when = _fmt_done_at(it.get("done_at", ""))
        ts = QLabel(when)
        ts.setProperty("class", "donetime")
        row.addWidget(ts)

        w = QWidget()
        w.setLayout(row)
        return w

    def _row(self, it):
        """一条待处理任务：浅灰圆角块 + 圆形空心勾选框 + 文字 + 置顶胶囊 + 删除。
        样式贴 JoyChat 参考图。"""
        row = QHBoxLayout()
        row.setSpacing(10)
        row.setContentsMargins(12, 9, 12, 9)

        # 圆形空心勾选框：点一下 = 完成
        done = QPushButton("")
        done.setFixedSize(20, 20)
        done.setCursor(Qt.PointingHandCursor)
        done.setToolTip("完成")
        done.setStyleSheet(
            "QPushButton { border:1.5px solid #cdcdcd; border-radius:10px;"
            " background:transparent; }"
            "QPushButton:hover { border-color:#1a1a1a; }")
        done.clicked.connect(lambda: self.app.mark_done(it["id"]))
        row.addWidget(done)

        lbl = QLabel(it["text"])
        lbl.setProperty("class", "task")
        lbl.setFixedWidth(260)
        lbl.setWordWrap(True)
        lbl.setToolTip(it["text"])
        lbl.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)
        lbl.setMinimumHeight(lbl.sizeHint().height())
        row.addWidget(lbl, 1, Qt.AlignVCenter)

        # 置顶：灰色胶囊标签
        up = QPushButton("置顶")
        up.setCursor(Qt.PointingHandCursor)
        up.setToolTip("置顶")
        up.setStyleSheet(
            "QPushButton { color:#9a9a9a; font-size:12px; padding:3px 10px;"
            " border:1px solid #e3e3e3; border-radius:10px; background:transparent; }"
            "QPushButton:hover { color:#3a3a3a; border-color:#c8c8c8; }")
        up.clicked.connect(lambda: self.app.bump_top(it["id"]))
        row.addWidget(up)

        dele = QPushButton("✕")
        dele.setFixedSize(22, 22)
        dele.setToolTip("删除")
        dele.clicked.connect(lambda: self.app.delete(it["id"]))
        row.addWidget(dele)

        w = QWidget()
        w.setObjectName("taskcard")
        w.setStyleSheet(
            "#taskcard { background:#f6f6f6; border-radius:14px; }")
        w.setLayout(row)
        w.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Minimum)
        w.setMinimumHeight(w.sizeHint().height())
        return w

    def _mention_row(self, c):
        """一条被@候选：灰底圆角卡片。首行「发送人 · 群名右对齐」，
        中间 @ 内容，底部「收下」(黑) + 「忽略」(白描边)。贴 JoyChat 参考图。"""
        key = c.get("key")
        box = QVBoxLayout()
        box.setSpacing(8)
        box.setContentsMargins(12, 11, 12, 11)

        # 首行：发送人（左）+ 群名（右，淡色）
        top = QHBoxLayout()
        top.setSpacing(6)
        top.setContentsMargins(0, 0, 0, 0)
        who = QLabel(c.get("sender", "?"))
        who.setProperty("class", "mfrom")
        who.setMaximumWidth(160)
        top.addWidget(who)
        top.addStretch(1)
        grp = QLabel(c.get("group_name", ""))
        grp.setObjectName("count")
        grp.setMaximumWidth(120)
        top.addWidget(grp)
        tw = QWidget()
        tw.setLayout(top)
        box.addWidget(tw)

        # @ 内容正文：候选区本身可滚动，这里展示全文，不提前截断。
        body = QLabel(compact_text(c.get("content", "")))
        body.setProperty("class", "mbody")
        body.setFixedWidth(280)
        body.setWordWrap(True)
        body.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Minimum)
        body.setMinimumHeight(body.sizeHint().height())
        box.addWidget(body)

        # 底部按钮：收下（黑，占宽）+ 忽略（白描边）
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.setContentsMargins(0, 0, 0, 0)
        accept = QPushButton("收下")
        accept.setObjectName("accept")
        accept.setCursor(Qt.PointingHandCursor)
        accept.setToolTip("转为待办事项")
        accept.clicked.connect(lambda: self.app.accept_mention(key))
        btns.addWidget(accept, 1)

        ignore = QPushButton("忽略")
        ignore.setObjectName("ignore")
        ignore.setCursor(Qt.PointingHandCursor)
        ignore.setToolTip("丢弃，不再拉回")
        ignore.clicked.connect(lambda: self.app.ignore_mention(key))
        btns.addWidget(ignore, 1)
        bw = QWidget()
        bw.setLayout(btns)
        box.addWidget(bw)

        w = QWidget()
        w.setObjectName("mcard")
        w.setStyleSheet("#mcard { background:#f6f6f6; border-radius:14px; }")
        w.setLayout(box)
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        w.setMinimumHeight(w.sizeHint().height())
        return w


class GroupManagerDialog(QDialog):
    """管理监控群：查看/添加/删除群，以及设置 @我 检测姓名。
    加群走 app.add_group_manually（直接填群号），删群清游标。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("管理监控群")
        self.setMinimumWidth(360)
        self.setStyleSheet(DIALOG_QSS)
        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(16, 16, 16, 16)
        self.vbox.setSpacing(8)
        self._reload()

    def _clear(self):
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _reload(self):
        self._clear()

        # @检测姓名一行
        namerow = QHBoxLayout()
        namerow.addWidget(QLabel("检测姓名："))
        nv = QLabel(self.app.mention_name() or "未设置")
        nv.setObjectName("nameval")
        namerow.addWidget(nv)
        namerow.addStretch(1)
        nbtn = QPushButton("修改")
        nbtn.setObjectName("name")
        nbtn.clicked.connect(self._edit_name)
        namerow.addWidget(nbtn)
        nrw = QWidget()
        nrw.setLayout(namerow)
        self.vbox.addWidget(nrw)

        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet("color:#ece3d4;")
        self.vbox.addWidget(div)

        # 群列表
        groups = self.app.monitor_groups()
        if not groups:
            hint = QLabel("还没加群，点下面「添加群」")
            hint.setObjectName("hint")
            self.vbox.addWidget(hint)
        else:
            for g in groups:
                row = QHBoxLayout()
                lbl = QLabel(f"{g.get('name', '')}（{g.get('id', '')}）")
                lbl.setObjectName("gname")
                lbl.setMaximumWidth(260)
                row.addWidget(lbl, 1)
                rm = QPushButton("移除")
                rm.setObjectName("rm")
                rm.clicked.connect(lambda _=False, gid=g.get("id"): self._on_remove(gid))
                row.addWidget(rm)
                rw = QWidget()
                rw.setLayout(row)
                self.vbox.addWidget(rw)

        # 检查频率一行（原来在主菜单，挪到这里统一管理）
        div2 = QFrame()
        div2.setFrameShape(QFrame.HLine)
        div2.setStyleSheet("color:#ece3d4;")
        self.vbox.addWidget(div2)
        irow = QHBoxLayout()
        irow.addWidget(QLabel("检查频率："))
        iv = QLabel(f"{self.app.poll_interval_min()} 分钟")
        iv.setObjectName("nameval")
        irow.addWidget(iv)
        irow.addStretch(1)
        ibtn = QPushButton("修改")
        ibtn.setObjectName("name")
        ibtn.clicked.connect(self._edit_interval)
        irow.addWidget(ibtn)
        irw = QWidget()
        irw.setLayout(irow)
        self.vbox.addWidget(irw)

        # 底部按钮
        btns = QHBoxLayout()
        add = QPushButton("＋ 添加群")
        add.setObjectName("add")
        add.clicked.connect(self._on_add)
        btns.addWidget(add)
        btns.addStretch(1)
        close = QPushButton("知道了")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        bw = QWidget()
        bw.setLayout(btns)
        self.vbox.addWidget(bw)
        self.adjustSize()

    def _edit_name(self):
        cur = self.app.mention_name()
        text, ok = SimpleInputDialog.get_text(
            self.app, "设置检测姓名", "检测「@这个名字」的消息：", text=cur)
        if ok:
            self.app.mentions["mention_name"] = text.strip()
            save_mentions(self.app.mentions)
            self._reload()

    def _edit_interval(self):
        # 复用 app 的检查频率设置逻辑（弹输入框+写配置+重启定时器），改完刷新本弹窗
        self.app._edit_interval(parent=self)
        self._reload()

    def _on_add(self):
        # 直接填群号加群；成功后回调里刷新本弹窗
        self.app.add_group_manually(parent=self, on_added=self._reload)

    def _on_remove(self, gid):
        groups = [g for g in self.app.monitor_groups() if g.get("id") != gid]
        self.app.mentions["monitor_groups"] = groups
        # 一并清掉该群游标：否则再加回来不会走冷启动基线，
        # 会把上次游标之后的历史 @一次性当新的弹出来。
        self.app.mentions.get("last_since", {}).pop(gid, None)
        save_mentions(self.app.mentions)
        self._reload()
        self.app.update_badge()


class ReminderManagerDialog(QDialog):
    """定时提醒管理：列出已有提醒（时间+文本+启用/删除），可新增。
    每条提醒每天到点触发一次。"""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setWindowTitle("提醒事项")
        self.setMinimumWidth(340)
        self.setStyleSheet(DIALOG_QSS)
        self.vbox = QVBoxLayout(self)
        self.vbox.setContentsMargins(16, 16, 16, 16)
        self.vbox.setSpacing(8)
        self._reload()

    def _clear(self):
        while self.vbox.count():
            item = self.vbox.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _reload(self):
        self._clear()
        reminders = sorted(self.app.reminders_all(), key=lambda r: r.get("time", ""))
        if not reminders:
            hint = QLabel("目前还没有提醒事项。\n你可以点击「新增提醒」，让我在合适的时间轻轻提醒你。")
            hint.setObjectName("hint")
            self.vbox.addWidget(hint)
        else:
            for r in reminders:
                row = QHBoxLayout()
                row.setSpacing(6)
                t = QLabel(r.get("time", ""))
                t.setObjectName("rtime")
                row.addWidget(t)
                on = r.get("enabled", True)
                txt = QLabel(self.app._clip(r.get("text", ""), 18))
                txt.setObjectName("rtext" if on else "rtext_off")
                txt.setMaximumWidth(170)
                row.addWidget(txt, 1)
                tg = QPushButton("开" if on else "关")
                tg.setObjectName("tg")
                tg.setToolTip("点击切换启用/停用")
                tg.clicked.connect(lambda _=False, rid=r.get("id"), e=on:
                                   self._toggle(rid, not e))
                row.addWidget(tg)
                rm = QPushButton("删除")
                rm.setObjectName("rm")
                rm.clicked.connect(lambda _=False, rid=r.get("id"): self._remove(rid))
                row.addWidget(rm)
                rw = QWidget()
                rw.setLayout(row)
                self.vbox.addWidget(rw)

        btns = QHBoxLayout()
        add = QPushButton("＋ 新增提醒")
        add.setObjectName("add")
        add.clicked.connect(self._add)
        btns.addWidget(add)
        btns.addStretch(1)
        close = QPushButton("稍后再说")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        bw = QWidget()
        bw.setLayout(btns)
        self.vbox.addWidget(bw)
        self.adjustSize()

    def _add(self):
        # 先填时间（HH:MM），再填提醒内容
        tstr, ok = SimpleInputDialog.get_text(
            self.app, "提醒时间", "几点提醒？（24 小时制，如 12:00 或 18:30）：")
        if not ok or not tstr.strip():
            return
        tstr = _normalize_hhmm(tstr.strip())
        if tstr is None:
            QMessageBox.warning(self, "格式不对", "请输入 HH:MM，例如 12:00")
            return
        text, ok = SimpleInputDialog.get_text(
            self.app, "提醒内容", f"每天 {tstr} 提醒你：", text="吃饭啦")
        if not ok or not text.strip():
            return
        self.app.add_reminder(tstr, text.strip())
        self._reload()

    def _remove(self, rid):
        self.app.remove_reminder(rid)
        self._reload()

    def _toggle(self, rid, enabled):
        self.app.toggle_reminder(rid, enabled)
        self._reload()


# =========================== 漫画提醒气泡 ===========================

class ReminderBubble(QWidget):
    """到点弹的漫画对话气泡：圆角卡片 + 朝下小尾巴，挂在小精灵上方。
    点击任意处或 10 秒后自动消失。"""

    def __init__(self, text, on_close):
        super().__init__()
        self._on_close = on_close
        # 同 QueueBubble：关掉系统投影，避免半透明窗口外缘的黑框描边。
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        try:
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        except Exception:
            pass

        self._pad = 16      # 卡片内边距
        self._tail = 12     # 尾巴高度
        self._margin = 20   # 四周留给阴影/尾巴的透明边
        self._text = text

        # 文本标签（放在卡片内）
        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(
            "color:#33302b; font-size:14px; font-weight:600; background:transparent;")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMaximumWidth(240)

        # 依文本算尺寸
        self.label.adjustSize()
        lw = min(240, max(120, self.label.sizeHint().width()))
        self.label.setFixedWidth(lw)
        self.label.adjustSize()
        lh = self.label.height()
        cw = lw + self._pad * 2
        ch = lh + self._pad * 2
        self._card_w, self._card_h = cw, ch
        self.resize(cw + self._margin * 2,
                    ch + self._tail + self._margin * 2)
        self.label.move(self._margin + self._pad, self._margin + self._pad)

        # 10 秒自动关
        self._auto = QTimer(self)
        self._auto.setSingleShot(True)
        self._auto.timeout.connect(self._close)
        self._auto.start(10 * 1000)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        m = self._margin
        cw, ch, tail = self._card_w, self._card_h, self._tail
        path = QPainterPath()
        rect = QRectF(m, m, cw, ch)
        path.addRoundedRect(rect, 16, 16)
        # 朝下的小尾巴（漫画对话框）
        cx = m + cw / 2
        by = m + ch
        tri = QPolygonF([
            QPointF(cx - 11, by - 1),
            QPointF(cx + 11, by - 1),
            QPointF(cx, by + tail),
        ])
        path.addPolygon(tri)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(252, 247, 238, 250)))
        p.drawPath(path.simplified())
        # 一圈淡描边
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(232, 163, 60, 180), 2))
        p.drawRoundedRect(rect, 16, 16)

    def mousePressEvent(self, e):
        self._close()

    def _close(self):
        if self._auto.isActive():
            self._auto.stop()
        if self._on_close:
            cb, self._on_close = self._on_close, None
            cb()
        self.close()


# =========================== 小精灵主窗口 ===========================

class Pet(QWidget):
    def __init__(self):
        super().__init__()
        self.items = load_items()
        self.mentions = load_mentions()
        self.reminders = load_reminders()

        # NoDropShadowWindowHint 是关键：半透明窗口在 macOS 上会由合成器按窗口
        # alpha 蒙版生成投影，软边缘处渲染成一圈浅色 halo（跳动/放大时最明显），
        # 徽标那团独立色块还会投出一个单独的浅色圆圈。关掉窗口投影即彻底消除。
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        try:
            self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        except Exception:
            pass

        # 小精灵图片：预加载睁眼/闭眼两帧。
        # Retina 屏(dpr=2)下必须缩放到「物理像素」再标记 devicePixelRatio，
        # 否则系统会把 dpr=1 的位图二次放大，半透明边缘插值出白色 halo（白底假象）。
        dpr = self.screen().devicePixelRatio() if self.screen() \
            else QGuiApplication.primaryScreen().devicePixelRatio()
        self._pix_open = self._load_pet_pixmap(ASSET_PET, dpr)
        self._pix_blink = self._load_pet_pixmap(ASSET_PET_BLINK, dpr)

        # devicePixelRatio 已写进 pixmap，逻辑尺寸 = 物理像素 / dpr
        if not self._pix_open.isNull():
            s = self._pix_open.deviceIndependentSize()
            self._pet_size = QSize(round(s.width()), round(s.height()))
        else:
            self._pet_size = QSize(PET_WIDTH, 150)

        # 窗口比图片四周各留 MARGIN，供浮动/放大/跳动时溢出，不移动窗口本身
        pw, ph = self._pet_size.width(), self._pet_size.height()
        extra = int(pw * (HOVER_SCALE - 1)) + JUMP_AMP + FLOAT_AMP + 8
        self._margin = extra
        self.resize(pw + extra * 2, ph + extra * 2)

        # 小精灵由 paintEvent 直接绘制（见 _pet_rect / paintEvent），不用 QLabel。
        # 全自绘让缩放/浮动/跳动都在同一层做正确的 alpha 合成，几何最干净；
        # 徽标也画在同层（见下）。（白色 halo 的真正成因是窗口投影，已由
        # NoDropShadowWindowHint 解决，见上方 setWindowFlags。）
        self._pet_rect = QRectF()   # 当前帧小人绘制矩形（逻辑坐标）

        # 待办计数（画在 paintEvent 里，不用 QLabel）：与小人同层绘制，
        # 避免独立子控件带来额外的合成层与几何错位。
        self._badge_count = 0

        self.bubble = QueueBubble(self)
        self.bubble.hide()

        self._menu = self._build_menu()
        self._press_pos = None
        self._moved = False
        self._single_click_pending = False

        # ---------- 动画状态 ----------
        self._t = 0.0            # 动画时钟（秒）
        self._scale = 1.0        # 当前缩放
        self._scale_target = 1.0  # 目标缩放（悬停切换）
        self._jump = 0.0         # 当前跳动偏移（衰减振荡）
        self._blinking = False

        # 沙漏动画：悬停触发一次「漏沙 → 翻转」，鼠标移开复位。
        # phase: "idle"→待机满上舱；"drain"→上舱漏到下舱；"flip"→整体翻180°；"done"→停住
        self._hg_phase = "idle"
        self._hg_t = 0.0         # 当前阶段已进行时间（秒）
        self._hg_flip = 0.0      # 沙漏翻转角度进度 0→1（1=已翻180°）

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(33)     # ~30fps

        self._blink_timer = QTimer(self)
        self._blink_timer.setSingleShot(True)
        self._blink_timer.timeout.connect(self._do_blink)
        self._schedule_blink()

        self.setMouseTracking(True)
        self._layout_pet()

        self._restore_position()
        self.update_badge()

        # ---------- 被@轮询 ----------
        # poller 在独立 QThread 里跑阻塞的 joyctl（约 14 秒），完成后用信号回主线程。
        self._poll_thread = None
        self._poller = None
        self._checking = False   # 「立即检查」进行中标志，供面板显示「检查中…」
        self._poll_timer = QTimer(self)
        # 注意：QTimer.timeout 会给槽传一个 bool，会污染 _start_poll(manual)，
        # 故用 lambda 显式调用，确保定时轮询走 manual=False。
        self._poll_timer.timeout.connect(lambda: self._start_poll())
        # 永远起定时器+排首轮：没配群时 _start_poll 会早退，零浪费；
        # 用户后来加了第一个群，下个 tick 自动开始轮询，不用重启。
        self._poll_timer.start(self.poll_interval_min() * 60 * 1000)
        QTimer.singleShot(POLL_FIRST_DELAY_MS, lambda: self._start_poll())

        # ---------- 定时提醒 ----------
        self._reminder_popup = None     # 当前弹出的漫画气泡（同时只留一个）
        self._reminder_timer = QTimer(self)
        self._reminder_timer.timeout.connect(self._check_reminders)
        self._reminder_timer.start(30 * 1000)   # 每 30 秒查一次到点没

    # ---------- 动画 ----------
    def _load_pet_pixmap(self, path, dpr):
        """按屏幕 dpr 缩放到物理像素，并标记 devicePixelRatio。
        逻辑显示宽仍是 PET_WIDTH，但位图分辨率吃满 Retina，避免边缘 halo。"""
        img = QImage(path)
        if img.isNull():
            return QPixmap()
        # 预乘 alpha：与半透明窗口合成路径一致，缩放插值时边缘更干净。
        img = img.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        img = img.scaledToWidth(
            int(round(PET_WIDTH * dpr)), Qt.SmoothTransformation)
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        return pm

    def _layout_pet(self):
        """按当前浮动/缩放/跳动，算出小人绘制矩形，然后触发重绘。"""
        pw, ph = self._pet_size.width(), self._pet_size.height()
        sw, sh = pw * self._scale, ph * self._scale   # 用浮点，避免取整错位
        float_y = math.sin(self._t * 2.0) * FLOAT_AMP
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        x = cx - sw / 2.0
        y = cy - sh / 2.0 + float_y - self._jump
        self._pet_rect = QRectF(x, y, sw, sh)
        self.update()   # 请求重绘

    def paintEvent(self, e):
        pm = self._pix_blink if self._blinking else self._pix_open
        if pm is None or pm.isNull() or self._pet_rect.isEmpty():
            return
        p = QPainter(self)
        # 平滑变换 + 高质量抗锯齿，让 Qt 一次性正确合成 alpha 边缘（无 halo）
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        # 源矩形用整张高分位图；目标矩形是浮点，drawPixmap 内部按 dpr 缩放
        p.drawPixmap(self._pet_rect, pm, QRectF(pm.rect()))
        # 沙漏动画已关闭（Joy 形象不需要）
        # self._draw_hourglass(p)
        # 待办计数气泡：画在小人右上角（同层绘制，无子控件 halo）
        if self._badge_count > 0:
            r = self._pet_rect
            d = 22.0
            bx = r.x() + r.width() - 24
            by = r.y() + 2
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor("#e8442e")))
            p.drawEllipse(QRectF(bx, by, d, d))
            p.setPen(QColor("white"))
            f = QFont(); f.setPixelSize(12); f.setBold(True)
            p.setFont(f)
            txt = str(self._badge_count) if self._badge_count < 100 else "99+"
            p.drawText(QRectF(bx, by, d, d), Qt.AlignCenter, txt)
        p.end()

    def _is_pet_hit(self, pos):
        """只让小人可见像素响应点击，避免透明留白误唤起面板。"""
        pm = self._pix_blink if self._blinking else self._pix_open
        if pm is None or pm.isNull() or self._pet_rect.isEmpty():
            return False
        point = QPointF(pos)
        if not self._pet_rect.contains(point):
            return False

        rx = (point.x() - self._pet_rect.x()) / max(1.0, self._pet_rect.width())
        ry = (point.y() - self._pet_rect.y()) / max(1.0, self._pet_rect.height())
        img = pm.toImage()
        px = max(0, min(img.width() - 1, int(rx * img.width())))
        py = max(0, min(img.height() - 1, int(ry * img.height())))
        return img.pixelColor(px, py).alpha() > 20

    def _draw_hourglass(self, p):
        """在肚子的奶油圆底上画沙漏。按 _hg_phase/_hg_t 呈现漏沙，
        按 _hg_flip 呈现整体翻转（只翻沙漏，不动身体）。几何比例沿用
        make_pet.py 里原沙漏在 400×480 画布中的位置。"""
        r = self._pet_rect
        # 归一化坐标 → 当前帧像素坐标
        def X(fx): return r.x() + fx * r.width()
        def Y(fy): return r.y() + fy * r.height()
        # 沙漏关键比例（源画布 400×480）
        gx0, gx1 = 176 / 400, 224 / 400      # 玻璃上沿两端
        top_y, neck_y, bot_y = 283 / 480, 330 / 480, 377 / 480
        wood_top0, wood_top1 = 270 / 480, 283 / 480
        wood_bot0, wood_bot1 = 377 / 480, 390 / 480
        wx0, wx1 = 168 / 400, 232 / 400      # 木托两端
        cx = 200 / 400

        WOOD = QColor(176, 132, 80)
        SAND = QColor(232, 163, 60)
        GLASS = QColor(251, 243, 226)

        # 漏沙进度 d：0=全在上舱，1=全在下舱
        if self._hg_phase == "idle":
            d = 0.0
        elif self._hg_phase == "drain":
            d = min(1.0, self._hg_t / HG_DRAIN_DUR)
        else:                      # flip / done：沙子已全在（翻转前的）下舱
            d = 1.0

        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        # 整体翻转：绕沙漏中心旋转 flip*180°。只作用于沙漏。
        p.translate(X(cx), Y(neck_y))
        p.rotate(self._hg_flip * 180.0)
        p.translate(-X(cx), -Y(neck_y))

        def poly(pts):
            return QPolygonF([QPointF(X(a), Y(b)) for a, b in pts])

        # 玻璃两个锥体（上下对称，尖端在 neck）
        upper = poly([(gx0, top_y), (gx1, top_y), (cx, neck_y)])
        lower = poly([(cx, neck_y), (gx0, bot_y), (gx1, bot_y)])
        p.setPen(QPen(WOOD, max(1.0, r.width() * 0.005)))
        p.setBrush(QBrush(GLASS))
        p.drawPolygon(upper)
        p.drawPolygon(lower)

        # 上舱沙子：随 d 从满到空，顶面下沉、锥体收窄
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(SAND))
        if d < 1.0:
            # 顶面从 top_y 附近下降到 neck；用 (1-d) 决定剩余高度
            rem = 1.0 - d
            surf_y = top_y + (neck_y - top_y) * (1.0 - rem)
            half = (gx1 - gx0) / 2.0
            hw = half * rem            # 顶面半宽随剩余量收窄
            p.drawPolygon(poly([
                (cx - hw, surf_y), (cx + hw, surf_y), (cx, neck_y)]))
        # 下落的一线沙流（仅漏沙途中）
        if self._hg_phase == "drain" and 0.02 < d < 0.98:
            p.setPen(QPen(SAND, max(1.0, r.width() * 0.008)))
            p.drawLine(QPointF(X(cx), Y(neck_y)),
                       QPointF(X(cx), Y(bot_y - 0.01)))
            p.setPen(Qt.NoPen)
        # 下舱沙子：随 d 从空到满，从底部往上堆（下舱是尖朝上的三角）
        if d > 0.0:
            surf_y = bot_y + (neck_y - bot_y) * d   # d=1 时堆到 neck
            half = (gx1 - gx0) / 2.0
            hw = half * (1.0 - d)                   # 堆得越高，顶面越窄
            p.drawPolygon(poly([
                (cx - hw, surf_y), (cx + hw, surf_y),
                (gx1, bot_y), (gx0, bot_y)]))

        # 木托（画在最上层，翻转时一起转）
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(WOOD))
        p.drawPolygon(poly([
            (wx0, wood_top0), (wx1, wood_top0), (wx1, wood_top1), (wx0, wood_top1)]))
        p.drawPolygon(poly([
            (wx0, wood_bot0), (wx1, wood_bot0), (wx1, wood_bot1), (wx0, wood_bot1)]))
        p.restore()

    def _tick(self):
        self._t += 0.033
        # 悬停缩放：向目标平滑插值
        self._scale += (self._scale_target - self._scale) * 0.25
        # 点击跳动：衰减
        if abs(self._jump) > 0.5:
            self._jump *= 0.82
        else:
            self._jump = 0.0
        self._advance_hourglass()
        self._layout_pet()

    def _advance_hourglass(self):
        """推进沙漏动画：drain（漏沙）→ flip（翻转）→ done。"""
        dt = 0.033
        if self._hg_phase == "drain":
            self._hg_t += dt
            if self._hg_t >= HG_DRAIN_DUR:
                self._hg_phase = "flip"
                self._hg_t = 0.0
        elif self._hg_phase == "flip":
            self._hg_t += dt
            self._hg_flip = min(1.0, self._hg_t / HG_FLIP_DUR)
            if self._hg_t >= HG_FLIP_DUR:
                self._hg_phase = "done"

    def _schedule_blink(self):
        self._blink_timer.start(random.randint(2500, 6000))

    def _do_blink(self):
        if self._pix_blink.isNull():
            self._schedule_blink()
            return
        self._blinking = True
        QTimer.singleShot(140, self._end_blink)

    def _end_blink(self):
        self._blinking = False
        self._schedule_blink()

    def enterEvent(self, e):
        self._scale_target = HOVER_SCALE   # 悬停放大
        # 悬停触发一次「漏沙 → 翻转」，仅在待机态起步（避免重复触发）
        if self._hg_phase == "idle":
            self._hg_phase = "drain"
            self._hg_t = 0.0
            self._hg_flip = 0.0
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._scale_target = 1.0
        # 鼠标移开：复位沙漏，下次悬停可再放一次
        self._hg_phase = "idle"
        self._hg_t = 0.0
        self._hg_flip = 0.0
        super().leaveEvent(e)

    def _bounce(self):
        """点击/新增任务时跳一下。"""
        self._jump = JUMP_AMP

    def _toast(self, text):
        """在小精灵附近弹个短提示（用系统 tooltip，自动消失，零布局成本）。"""
        from PySide6.QtWidgets import QToolTip
        QToolTip.showText(self.frameGeometry().center(), text, self)

    # ---------- 菜单 ----------
    def _build_menu(self):
        m = QMenu(self)
        # 黑白极简：白底圆角、深色字、浅灰悬停高亮，与弹窗（DIALOG_QSS）同一套主题。
        m.setStyleSheet("""
            QMenu { background:#ffffff; border:1px solid #ececec;
                    border-radius:12px; padding:6px 8px; }
            QMenu::item { color:#1a1a1a; font-size:14px;
                          padding:9px 20px 9px 14px; border-radius:8px;
                          margin:1px 0; }
            QMenu::item:selected { background:#f0f0f0; color:#000000; }
            QMenu::item:checked { font-weight:600; }
            QMenu::separator { height:1px; background:#efefef;
                               margin:6px 12px; }
        """)
        a_add = QAction("添加待办", self)
        a_add.triggered.connect(self.add_via_dialog)
        m.addAction(a_add)
        a_remind = QAction("提醒事项", self)
        a_remind.triggered.connect(self.open_reminder_manager)
        m.addAction(a_remind)
        m.addSeparator()
        a_groups = QAction("管理监控群", self)
        a_groups.triggered.connect(self.open_group_manager)
        m.addAction(a_groups)
        a_check = QAction("立即检查 @我", self)
        a_check.triggered.connect(lambda: self._start_poll(manual=True))
        m.addAction(a_check)
        a_clear = QAction("清除已完成记录", self)
        a_clear.triggered.connect(self.clear_done)
        m.addAction(a_clear)
        self.a_launch = QAction("开机自动启动", self, checkable=True)
        self.a_launch.setChecked(launch_at_login_enabled())
        self.a_launch.triggered.connect(self.toggle_launch)
        m.addAction(self.a_launch)
        m.addSeparator()
        a_quit = QAction("退出", self)
        a_quit.triggered.connect(QApplication.quit)
        m.addAction(a_quit)
        return m

    # ---------- 数据操作 ----------
    def pending(self):
        return [it for it in self.items if it["status"] == "pending"]

    def done_recent(self):
        """已完成项，按完成时间倒序（最近的在前），最多 DONE_SHOW_MAX 条。"""
        dl = [it for it in self.items if it.get("status") == "done"]
        dl.sort(key=lambda it: it.get("done_at", ""), reverse=True)
        return dl[:DONE_SHOW_MAX]

    def add_via_dialog(self):
        text, ok = SimpleInputDialog.get_text(
            self, "Joy 待办", "添加一条待办：")
        if ok and text.strip():
            self.items.insert(0, new_item(text.strip()))
            save_items(self.items)
            self._bounce()         # 新增任务，开心跳一下
            self.refresh_ui()
            if not self.bubble.isVisible():
                self.toggle_bubble()

    def mark_done(self, item_id):
        for it in self.items:
            if it["id"] == item_id:
                it["status"] = "done"
                it["done_at"] = datetime.now().isoformat(timespec="seconds")
                break
        save_items(self.items)
        self.refresh_ui()

    def bump_top(self, item_id):
        idx = next((k for k, it in enumerate(self.items)
                    if it["id"] == item_id), None)
        if idx is not None:
            self.items.insert(0, self.items.pop(idx))
            save_items(self.items)
            self.refresh_ui()

    def delete(self, item_id):
        self.items = [it for it in self.items if it["id"] != item_id]
        save_items(self.items)
        self.refresh_ui()

    def clear_done(self):
        self.items = [it for it in self.items if it["status"] != "done"]
        save_items(self.items)
        self.refresh_ui()

    # ---------- 被@候选操作 ----------
    def candidates(self):
        return self.mentions.get("candidates", [])

    def poll_status(self):
        return self.mentions.get("poll_status", {})

    # ---------- 监控群 / 检测姓名配置 ----------
    def monitor_groups(self):
        return self.mentions.get("monitor_groups", [])

    def monitor_group_ids(self):
        return [g["id"] for g in self.monitor_groups() if g.get("id")]

    def mention_name(self):
        return compact_text(self.mentions.get("mention_name") or MENTION_NAME)

    def poll_interval_min(self):
        """轮询间隔（分钟）：读配置，缺省 10，钳在 [MIN, MAX] 内。"""
        try:
            v = int(self.mentions.get("poll_interval_min", 10))
        except (TypeError, ValueError):
            v = 10
        return max(POLL_INTERVAL_MIN, min(v, POLL_INTERVAL_MAX))

    def _restart_poll_timer(self, minutes):
        """按指定分钟数重启轮询定时器，统一处理边界。"""
        minutes = max(POLL_INTERVAL_MIN, min(int(minutes), POLL_INTERVAL_MAX))
        self._poll_timer.start(minutes * 60 * 1000)

    # ---------- 定时提醒 ----------
    def _check_reminders(self):
        """每 30 秒查一次：有到点且今天还没弹过的提醒就弹气泡。"""
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        today = now.strftime("%Y-%m-%d")
        changed = False
        for r in self.reminders:
            if not r.get("enabled", True):
                continue
            if r.get("time") == hhmm and r.get("last_fired") != today:
                r["last_fired"] = today
                changed = True
                self._fire_reminder(r.get("text", "该活动一下啦"))
        if changed:
            save_reminders(self.reminders)

    def _fire_reminder(self, text):
        """到点：弹漫画气泡 + 小精灵跳动。"""
        self._bounce()
        self._show_reminder_bubble(text)

    def reminders_all(self):
        return self.reminders

    def add_reminder(self, time_str, text):
        self.reminders.append(new_reminder(time_str, text))
        save_reminders(self.reminders)

    def remove_reminder(self, rid):
        self.reminders = [r for r in self.reminders if r.get("id") != rid]
        save_reminders(self.reminders)

    def toggle_reminder(self, rid, enabled):
        for r in self.reminders:
            if r.get("id") == rid:
                r["enabled"] = enabled
                break
        save_reminders(self.reminders)

    def _dismiss_key(self, key):
        """记入 dismissed_keys（去重 + 截断），下次轮询不再拉回。"""
        if not key:
            return
        dk = self.mentions.setdefault("dismissed_keys", [])
        if key not in dk:
            dk.append(key)
        if len(dk) > DISMISSED_KEEP:
            del dk[:-DISMISSED_KEEP]

    def _pop_candidate(self, key):
        cands = self.mentions.get("candidates", [])
        found = next((c for c in cands if c.get("key") == key), None)
        self.mentions["candidates"] = [c for c in cands if c.get("key") != key]
        return found

    def accept_mention(self, key):
        """候选「收下」→ 转成正式的「待办事项」任务，插到队列顶部。"""
        c = self._pop_candidate(key)
        if c:
            summary = compact_text(c.get("content", ""))
            text = f"{c.get('sender', '?')}@我·{c.get('group_name', '')}：{summary}"
            self.items.insert(0, new_item(text))
            save_items(self.items)
            self._dismiss_key(key)
            save_mentions(self.mentions)
            self._bounce()
        self.refresh_ui()

    def accept_all_mentions(self):
        """批量「全部收下」：按当前展示顺序转成待办，插到队列顶部。"""
        cands = list(self.mentions.get("candidates", []))
        if not cands:
            return

        new_items = []
        for c in cands:
            summary = compact_text(c.get("content", ""))
            text = f"{c.get('sender', '?')}@我·{c.get('group_name', '')}：{summary}"
            new_items.append(new_item(text))
            self._dismiss_key(c.get("key"))

        self.items[0:0] = new_items
        self.mentions["candidates"] = []
        save_items(self.items)
        save_mentions(self.mentions)
        self._bounce()
        self.refresh_ui()

    def ignore_mention(self, key):
        """候选「忽略」→ 丢弃，且记住别再拉回。"""
        if self._pop_candidate(key):
            self._dismiss_key(key)
            save_mentions(self.mentions)
        self.refresh_ui()

    def clear_mentions(self):
        """批量「清空」：忽略当前所有候选，且记住别再拉回。"""
        cands = list(self.mentions.get("candidates", []))
        if not cands:
            return

        for c in cands:
            self._dismiss_key(c.get("key"))
        self.mentions["candidates"] = []
        save_mentions(self.mentions)
        self.refresh_ui()

    @staticmethod
    def _clip(text, n=40):
        t = compact_text(text)   # 去换行/多空格，仅用于明确需要摘要的地方
        return t if len(t) <= n else t[:n] + "…"

    # ---------- 被@轮询 ----------
    def _start_poll(self, manual=False):
        """轮询被@。manual=True 表示用户点了「立即检查」，需要即时反馈：
        正忙就提示、否则立刻在面板显示「检查中…」，别让用户以为没反应。"""
        if self._poll_thread is not None:   # 上一轮还在跑
            if manual:
                self._checking = True       # 复用「检查中」提示，点击有反馈
                self.refresh_ui()
            return
        if not self.monitor_group_ids():    # 没配群，无事可做
            if manual:
                self._toast("还没加监控群，先去「管理监控群」加一个")
            return
        if not self.mention_name():
            self.mentions["poll_status"] = {
                "ok": False,
                "at": datetime.now().strftime("%m-%d %H:%M"),
                "error": "未设置 @ 检测姓名",
                "mention_name_missing": True,
                "fail_count": 0,
                "alert": True,
            }
            save_mentions(self.mentions)
            if manual:
                self._toast("先在「管理监控群」里设置你的 @ 名称")
            self.refresh_ui()
            return
        if manual:
            self._checking = True           # 面板显示「检查中…」
            self.refresh_ui()
        thread = QThread(self)
        poller = MentionPoller()
        poller.since_map = dict(self.mentions.get("last_since", {}))
        poller.groups = self.monitor_group_ids()
        poller.mention_name = self.mention_name()
        known = {c.get("key") for c in self.candidates()}
        known |= set(self.mentions.get("dismissed_keys", []))
        poller.known_keys = known
        poller.moveToThread(thread)
        thread.started.connect(poller.poll)
        poller.found.connect(self._on_mentions_found)
        poller.finished.connect(thread.quit)
        # 线程收尾：清引用，允许下一轮
        thread.finished.connect(poller.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_poll_finished)
        # 关键：持有 poller 引用，否则本方法返回后它会被 GC，
        # 跨线程的 started→poll 信号还没投递，poll 就永远不会跑。
        self._poll_thread = thread
        self._poller = poller
        thread.start()

    def _on_poll_finished(self):
        self._poll_thread = None
        self._poller = None
        self._checking = False   # 清「检查中…」
        self.refresh_ui()

    def _on_mentions_found(self, new_candidates, updated_since, errors):
        # 全部在主线程：写文件 + 刷 UI
        self.mentions["last_since"] = updated_since
        if new_candidates:
            self.mentions.setdefault("candidates", []).extend(new_candidates)
        # 记录本轮轮询结果，供面板/菜单显示（打包成 .app 后看不到 stderr）
        now = datetime.now().strftime("%m-%d %H:%M")
        prev = self.mentions.get("poll_status", {})
        if errors == [JOYCTL_MISSING]:
            # joyctl 没装：确定性问题，不走「连续失败才报警」的阈值，
            # 立即在面板显示安装引导。
            self.mentions["poll_status"] = {
                "ok": False, "at": now, "error": "",
                "joyctl_missing": True,
                "fail_count": 0, "alert": True}
        elif errors:
            # 累计连续失败次数；达到阈值才在面板亮红，避免偶发一次超时就报警
            fail_count = int(prev.get("fail_count", 0)) + 1
            error_text = "；".join(errors)[:300]
            error_kind = classify_poll_error(error_text)
            backoff_min = min(
                POLL_BACKOFF_MAX_MIN,
                self.poll_interval_min() * (2 ** min(max(fail_count - 1, 0), 4)),
            )
            self.mentions["poll_status"] = {
                "ok": False, "at": now, "error": error_text,
                "error_kind": error_kind,
                "login_expired": error_kind == "login_expired",
                "fail_count": fail_count,
                "backoff_min": backoff_min,
                "alert": fail_count >= FAIL_ALERT_THRESHOLD,
            }
            self._restart_poll_timer(backoff_min)
        else:
            # 一旦成功，清零失败计数
            self.mentions["poll_status"] = {
                "ok": True, "at": now, "error": "", "fail_count": 0,
                "alert": False}
            self._restart_poll_timer(self.poll_interval_min())
        save_mentions(self.mentions)
        if new_candidates:
            self._bounce()   # 有新的被@，跳一下提示
        self.refresh_ui()

    # ---------- 监控群管理 ----------
    def open_group_manager(self):
        """右键菜单「管理监控群…」：打开配置弹窗，关闭后刷新徽标与面板。"""
        dlg = GroupManagerDialog(self)
        dlg.adjustSize()
        center_on_pet_screen(dlg, self)
        dlg.exec()
        self.update_badge()
        self.refresh_ui()

    def open_reminder_manager(self):
        """右键菜单「提醒事项…」：打开提醒管理弹窗。"""
        dlg = ReminderManagerDialog(self)
        dlg.adjustSize()
        center_on_pet_screen(dlg, self)
        dlg.exec()

    def _edit_interval(self, parent=None):
        """自定义检查频率（分钟）：写入配置并立刻重启定时器生效。"""
        cur = self.poll_interval_min()
        val, ok = SimpleInputDialog.get_int(
            self, "检查频率",
            f"每隔多少分钟检查一次 @我（{POLL_INTERVAL_MIN}–{POLL_INTERVAL_MAX}）：",
            cur, POLL_INTERVAL_MIN, POLL_INTERVAL_MAX)
        if not ok:
            return
        self.mentions["poll_interval_min"] = val
        save_mentions(self.mentions)
        # 立刻重启定时器，新间隔当场生效，不用重启 app
        self._poll_timer.start(val * 60 * 1000)
        self._toast(f"已设为每 {val} 分钟检查一次")

    def add_group_manually(self, parent=None, on_added=None):
        """直接填群号加监控群：输入群号（可逗号/空格/换行分隔多个）→ 逐个
        写入 monitor_groups。不走 joyctl 搜索，无阻塞、无卡死。列表里群名
        直接显示群号。on_added 在加群后回调。"""
        text, ok = SimpleInputDialog.get_text(
            self, "添加监控群",
            "输入群号（可用逗号、空格或换行分隔多个）：")
        if not ok or not text.strip():
            return

        # 拆出所有群号：逗号/空格/换行/中文逗号都当分隔符
        raw = re.split(r"[,\s，]+", text.strip())
        ids = [x.strip() for x in raw if x.strip()]
        if not ids:
            return

        existing = set(self.monitor_group_ids())
        groups = self.monitor_groups()
        added = 0
        for gid in ids:
            if gid in existing:
                continue
            # 群名先用群号占位
            groups.append({"id": gid, "name": gid})
            existing.add(gid)
            added += 1

        if added == 0:
            QMessageBox.information(parent, "已在监控", "填的群号都已经在监控列表里了。")
            return

        self.mentions["monitor_groups"] = groups
        save_mentions(self.mentions)
        self.update_badge()
        if on_added:
            on_added()

    # ---------- 刷新 ----------
    def refresh_ui(self):
        # 推迟到本次点击事件处理完再重建，避免销毁"正被点击的按钮"导致崩溃
        QTimer.singleShot(0, self._do_refresh)

    def _do_refresh(self):
        # 内容数量变化后必须重新布局和定位；否则删除待办后旧窗口区域会残留成白屏。
        self.bubble.refresh()
        QApplication.processEvents()
        self.bubble._finish_layout()
        if self.bubble.isVisible():
            self._place_bubble()
            self.bubble.raise_()
        self.update_badge()

    def update_badge(self):
        # 待办 + 被@候选都算「等你处理的事」，合并计数
        self._badge_count = len(self.pending()) + len(self.candidates())
        self.update()

    # ---------- 气泡显隐与定位 ----------
    def toggle_bubble(self):
        if self.bubble.isVisible():
            self.bubble.hide()
        else:
            self.bubble.refresh()
            self._place_bubble()
            self.bubble.show()
            self.bubble.raise_()

    def _screen_geo(self):
        """小精灵当前所在屏幕的可用区域。多屏时必须跟随小人所在屏，
        否则用 primaryScreen 会把气泡钳回主屏（拖到别的屏后气泡跟不过去）。"""
        scr = (self.screen()
               or QGuiApplication.screenAt(self.frameGeometry().center())
               or QApplication.primaryScreen())
        return scr.availableGeometry()

    def _place_bubble(self):
        # 放在小精灵上方；若上方空间不够则放下方
        # 窗口四周有透明 margin，小人图在窗口中心，故按小人视觉区域定位
        self.bubble.adjustSize()
        bw, bh = self.bubble.width(), self.bubble.height()
        g = self.frameGeometry()
        pet_top = g.center().y() - self._pet_size.height() // 2
        pet_bottom = g.center().y() + self._pet_size.height() // 2
        x = g.center().x() - bw // 2
        y = pet_top - bh + 10
        screen = self._screen_geo()
        if y < screen.top():
            y = pet_bottom - 10
        x = max(screen.left() + 4, min(x, screen.right() - bw - 4))
        self.bubble.move(x, y)

    def _show_reminder_bubble(self, text):
        """在小精灵上方弹漫画气泡；同时只留一个，新的替换旧的。"""
        if self._reminder_popup is not None:
            try:
                self._reminder_popup.close()
            except Exception:
                pass
            self._reminder_popup = None

        def _cleared():
            self._reminder_popup = None

        pop = ReminderBubble(text, on_close=_cleared)
        bw, bh = pop.width(), pop.height()
        g = self.frameGeometry()
        pet_top = g.center().y() - self._pet_size.height() // 2
        pet_bottom = g.center().y() + self._pet_size.height() // 2
        x = g.center().x() - bw // 2
        y = pet_top - bh + 10          # 气泡尾巴朝下指向小人
        screen = self._screen_geo()
        if y < screen.top():
            y = pet_bottom - 10        # 上方不够就放下方
        x = max(screen.left() + 4, min(x, screen.right() - bw - 4))
        pop.move(x, y)
        pop.show()
        pop.raise_()
        self._reminder_popup = pop

    # ---------- 位置记忆 ----------
    def _restore_position(self):
        st = load_state()
        screen = QApplication.primaryScreen().availableGeometry()
        if "x" in st and "y" in st:
            self.move(int(st["x"]), int(st["y"]))
        else:
            self.move(screen.right() - self.width() - 40,
                      screen.bottom() - self.height() - 40)
        self._ready = True   # 之后的移动才开始记忆

    # ---------- 鼠标：系统级拖动 / 点击 / 右键 ----------
    def mousePressEvent(self, e):
        if not self._is_pet_hit(e.position().toPoint()):
            self._click_candidate = False
            e.ignore()
            return
        if e.button() == Qt.RightButton:
            self._menu.exec(e.globalPosition().toPoint())
            return
        if e.button() == Qt.LeftButton:
            self._press_global = e.globalPosition().toPoint()
            self._click_candidate = True

    def mouseMoveEvent(self, e):
        if not getattr(self, "_click_candidate", False):
            return
        moved = (e.globalPosition().toPoint()
                 - self._press_global).manhattanLength()
        if moved > 6:
            # 交给系统来拖，macOS 上无边框窗口这样才拖得动
            self._click_candidate = False
            wh = self.windowHandle()
            if wh is not None:
                wh.startSystemMove()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and getattr(self, "_click_candidate", False):
            self._click_candidate = False
            self._single_click_pending = True
            # 双击会先经历一次单击 release；延迟确认，给 doubleClickEvent 取消机会。
            QTimer.singleShot(QApplication.doubleClickInterval() + 30,
                              self._commit_single_click)

    def _commit_single_click(self):
        if not self._single_click_pending:
            return
        self._single_click_pending = False
        self._bounce()         # 点一下先跳一下
        self.toggle_bubble()   # 没拖动 = 单击，展开/收起队列

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton and self._is_pet_hit(e.position().toPoint()):
            self._click_candidate = False
            self._single_click_pending = False
            self._menu.exec(e.globalPosition().toPoint())
            return
        e.ignore()

    def moveEvent(self, e):
        super().moveEvent(e)
        if getattr(self, "_ready", False):
            save_state({"x": self.x(), "y": self.y()})
            if self.bubble.isVisible():
                self._place_bubble()


    # ---------- 菜单动作 ----------
    def toggle_launch(self, checked):
        ok = set_launch_at_login(checked)
        if not ok:
            self.a_launch.setChecked(False)


def _augment_path():
    """把常见的 node/npm bin 目录补进 PATH。打包成 .app 经 Finder 启动时，
    PATH 被砍到只剩系统目录，joyctl 和它依赖的 node 都不在里面。这里主动补齐，
    让 shutil.which 和 joyctl 子进程（node 脚本，运行时还要找 node）都能工作。"""
    home = os.path.expanduser("~")
    extra = [
        "/opt/homebrew/bin", "/usr/local/bin",
        os.path.join(home, ".local/bin"),
        os.path.join(home, ".volta/bin"),
        os.path.join(home, "Library/pnpm"),
    ]
    for pat in [
        os.path.join(home, ".nvm/versions/node/*/bin"),
        os.path.join(home, ".fnm/node-versions/*/installation/bin"),
        os.path.join(home, "Library/Application Support/fnm/node-versions/*/installation/bin"),
    ]:
        extra += sorted(glob.glob(pat), reverse=True)
    cur = os.environ.get("PATH", "").split(os.pathsep)
    seen = set(cur)
    add = [d for d in extra if d not in seen and os.path.isdir(d)]
    if add:
        os.environ["PATH"] = os.pathsep.join(cur + add)


def main():
    _augment_path()   # 先补 PATH，joyctl 检测/调用才靠谱（尤其打包成 .app 后）
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # 隐藏气泡不退出

    # 全局错误捕获：任何异常都写进 error.log 并弹窗，方便定位
    error_log = os.path.join(DATA_DIR, "error.log")

    def excepthook(exc_type, exc, tb):
        # Ctrl+C 退出 / 正常退出：交给默认处理，不当成错误弹窗
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(exc_type, exc, tb)
            return
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        _ensure_dir()
        try:
            with open(error_log, "a", encoding="utf-8") as f:
                f.write(datetime.now().isoformat() + "\n" + msg + "\n")
        except Exception:
            pass
        sys.stderr.write(msg)
        try:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(None, "晚点队列出错了（已记录到 error.log）",
                                 msg[-1600:])
        except Exception:
            pass

    sys.excepthook = excepthook

    pet = Pet()
    pet.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
