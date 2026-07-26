"""
py2app 打包脚本 —— 在 Mac 上把桌宠打成可双击运行的 .app

用法（在本目录下，用装好依赖的同一个环境）：
    python setup.py py2app
    open dist/            # 把 LaterQueue.app 拖进"应用程序"

清理：rm -rf build dist
"""

from setuptools import setup

APP = ["laterqueue.py"]
DATA_FILES = [("assets", ["assets/pet.png", "assets/pet_blink.png"])]   # 打包时带上小精灵两帧

OPTIONS = {
    "argv_emulation": False,          # 新系统上开启常出问题，保持关闭
    "iconfile": "assets/AppIcon.icns",
    "packages": ["shiboken6"],
    # 只用到这三个 Qt 模块，其余不打包，大幅减小体积
    "includes": ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"],
    "excludes": [
        # 用不到的重型 Qt 模块（QtWebEngine 单独就几百 MB）
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.QtQml", "PySide6.QtQuick",
        "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
        "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtCharts", "PySide6.QtDataVisualization",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtDesigner",
        "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth",
        "PySide6.QtPositioning", "PySide6.QtSensors", "PySide6.QtSerialPort",
        "PySide6.QtWebSockets", "PySide6.QtNetworkAuth", "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets", "PySide6.QtWebChannel",
        # 无关的标准库
        "tkinter", "test", "unittest", "pydoc_data",
    ],
    "plist": {
        "CFBundleName": "LaterQueue",
        "CFBundleDisplayName": "晚点队列",
        "CFBundleIdentifier": "com.laterqueue.app",
        "CFBundleVersion": "0.3.0",
        "CFBundleShortVersionString": "0.3.0",
        "LSUIElement": True,          # 桌宠应用，不在 Dock 显示
    },
}

setup(
    app=APP,
    name="LaterQueue",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
