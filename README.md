# 晚点队列 · LaterQueue（桌面宠物版）

一只沙漏小精灵蹲在你桌面角落——**始终置顶、可拖动、不打断你**。
点它一下，冒出任务气泡：被打断时答应别人"晚点处理"的事都在里面，按优先级从上往下排；
有空时自己从里面"领"一件，做完打个勾。右键小精灵管添加、退出。

不弹通知、不催你——但它一直在余光里，让你不会忘、守得住那句"等我 20 分钟"。

> 用 PySide6（Qt）实现，参考桌宠交互；小精灵是原创形象，无版权顾虑。改 `make_pet.py` 可重画它。

---

## 跑起来

需要 macOS + Python 3.9+。venv 或 conda 都行。

**conda：**
```bash
cd laterqueue
conda create -n laterqueue python=3.11 -y
conda activate laterqueue
pip install -r requirements.txt
python laterqueue.py
```

**venv：**
```bash
cd laterqueue
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python laterqueue.py
```

启动后，小精灵会出现在屏幕右下角。回终端 Ctrl+C，或右键小精灵 → 退出，即可关闭。

> Apple 芯片确保 Python 是 arm64：`python -c "import platform;print(platform.machine())"` 应为 `arm64`。

## 打包成可双击的 .app

```bash
cd laterqueue
python setup.py py2app        # 用上面装好依赖的同一个环境
open dist/                    # 把 LaterQueue.app 拖进"应用程序"
```

清理：`rm -rf build dist`。首次打开 .app 若提示"身份不明的开发者"：右键 App → 打开 → 确认（没做苹果签名）。

---

## 怎么用

- **拖动**：按住小精灵拖到你喜欢的角落，位置会被记住。
- **点一下小精灵**：展开 / 收起任务气泡。
- **添加**：气泡里点「＋ 添加一条」，或右键小精灵 →「添加一条」。敲一句话回车，新加的排**最上面**（最高优先级）。
- **领任务 / 做完**：有空时点开气泡从上往下挑；做完点那条的 **✓**，它就消失。
- **调优先级**：点某条的 **↑** 置顶。
- **删除**：点某条的 **✕**。
- **右上角小红点**：还欠着几件，一眼就知道。
- **右键菜单**：添加、显示/隐藏队列、清除已完成、开机自启、退出。

## 数据在哪

本地 JSON，纯离线、无账号、可手改。列表顺序 = 优先级（越靠上越优先）：

```
~/Library/Application Support/LaterQueue/queue.json     # 队列
~/Library/Application Support/LaterQueue/state.json      # 小精灵位置
```

## 卸载

- 删除 `LaterQueue.app`。
- 关自启（如开过）：右键取消勾选，或 `rm ~/Library/LaunchAgents/com.laterqueue.app.plist`。
- 删数据（可选）：`rm -rf ~/Library/Application\ Support/LaterQueue`。

---

## 设计取舍

- **桌宠而非通知**：通知是打断你（跟这套理念相悖）；小精灵是被动待在余光里，靠"看得见"防遗忘。
- **没有时间/提醒**：给每条设时间是"假精确"、是摩擦。改成优先级清单，有空自己领。
- **优先级 = 列表顺序**：新加置顶，手动 ↑ 调，不搞高/中/低档位。
- **刻意不做**：同步、云、协作、重复任务、标签——保持轻，才不会变形。

## 想改小精灵长相？

`make_pet.py` 里是它的全部画法（纯 PIL 几何图形）。改颜色/表情/沙漏后重跑：

```bash
python make_pet.py     # 重新生成 assets/pet.png
```

## 排错

- **看不到小精灵**：它默认在右下角；可能在别的屏幕/桌面。删掉 `state.json` 可重置位置。
- **`ModuleNotFoundError: PySide6`**：`pip install -r requirements.txt`（确认已 activate 对应环境）。
- **小精灵不在最前 / 切到别的 App 就没了**：代码已设 `WA_MacAlwaysShowToolWindow` 尽量常驻；打包成带 `LSUIElement` 的 .app 后表现最稳。
- **图片不显示（只有点击区域）**：确认 `assets/pet.png` 在；或重跑 `python make_pet.py`。
- **打包后闪退**：先 `python laterqueue.py` 跑通再打包；`rm -rf build dist` 重打。PySide6 用 py2app 打包偶有插件缺失，若卡住把报错发我。
