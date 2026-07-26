#!/usr/bin/env bash
# 一键把「晚点队列」打包成 macOS .app
# 用法：在项目目录下  bash build.sh   （或先 chmod +x build.sh 再 ./build.sh）
set -e
cd "$(dirname "$0")"

echo "▶ 准备虚拟环境…"
if [ ! -d venv ]; then
  python3 -m venv venv
fi
# 正常 venv 有 activate；conda 建的残缺 venv 没有，则直接用 venv 里的 python
if [ -f venv/bin/activate ]; then
  source venv/bin/activate
  PY=python
else
  echo "  （venv 无 activate，改用 venv/bin/python 直接调用）"
  PY=venv/bin/python
fi

echo "▶ 安装依赖（PySide6-Essentials 更小 + py2app）…"
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install PySide6-Essentials py2app setuptools >/dev/null

echo "▶ 先确认能正常启动（3 秒后自动关）…"
( "$PY" laterqueue.py & PID=$!; sleep 3; kill $PID 2>/dev/null ) || true

echo "▶ 清理旧产物并打包…"
# Finder 若开着 dist/ 会瞬时锁目录，导致 rm 偶发 EACCES；重试几次并容错
for d in build dist; do
  [ -e "$d" ] || continue
  rm -rf "$d" 2>/dev/null || { sleep 1; rm -rf "$d" 2>/dev/null; } || \
    chmod -R u+w "$d" 2>/dev/null && rm -rf "$d" 2>/dev/null || true
done
"$PY" setup.py py2app

echo "▶ 精简 bundle（删掉 py2app 强塞进来、本程序用不到的 Qt 组件）…"
SP=$(command find dist/LaterQueue.app/Contents/Resources/lib -maxdepth 2 -type d -name PySide6 | head -1)
if [ -n "$SP" ]; then
  # 整块用不到的目录：QML 运行时、翻译、示例、开发工具
  rm -rf "$SP/Qt/qml" "$SP/Qt/translations" "$SP/Qt/libexec" \
         "$SP/examples" "$SP/glue" "$SP/typesystems" \
         "$SP/Assistant.app" "$SP/Designer.app" "$SP/Linguist.app"
  rm -f "$SP/lupdate" "$SP/lrelease" "$SP/uic" "$SP/rcc" \
        "$SP/qmlformat" "$SP/qmllint" "$SP/qmlls"
  # 用不到的插件（保留 platforms/imageformats/styles/iconengines/tls）
  rm -rf "$SP/Qt/plugins/sqldrivers" "$SP/Qt/plugins/qmltooling" \
         "$SP/Qt/plugins/qmllint" "$SP/Qt/plugins/designer" \
         "$SP/Qt/plugins/generic" "$SP/Qt/plugins/networkinformation" \
         "$SP/Qt/plugins/vectorimageformats"
  # 用不到的 framework 和 .abi3.so（本程序只用 QtCore/QtGui/QtWidgets/QtDBus）
  for fw in QtQuick QtQml QtDesigner QtDesignerComponents QtQmlCompiler \
            QtQuickControls2Basic QtQuickControls2Fusion QtQuickControls2 \
            QtLabsStyleKit QtQmlModels QtOpenGL QtHelp QtPrintSupport QtLottie \
            QtLabsPlatform QtLabsStyleKitImpl QtQuickControls2FluentWinUI3StyleImpl \
            QtLottieVectorImageGenerator QtLabsQmlModels QtQuickControls2FusionStyleImpl \
            QtQmlMeta QtQmlXmlListModel QtQmlNetwork QtQmlCore QtLabsFolderListModel \
            QtQuickControls2BasicStyleImpl QtOpenGLWidgets QtQmlWorkerScript \
            QtQmlLocalStorage QtLabsSharedImage QtLabsAnimation QtLabsWavefrontMesh \
            QtLabsSynchronizer QtLottieVectorImageHelpers QtLabsSettings QtConcurrent \
            QtSql QtTest QtQuickWidgets QtUiTools QtQuick3D; do
    rm -rf "$SP/Qt/lib/$fw.framework"
    rm -f "$SP/$fw.abi3.so" "$SP/$fw.pyi"
  done
  echo "   精简后大小：$(du -sh dist/LaterQueue.app | cut -f1)"
fi

echo "▶ 补齐缺失的基础依赖库…"
# conda 的 Python 把 libffi/libssl/libz 等放在 base/lib，py2app 用 @rpath 引用却常漏收，
# 导致启动时 dlopen 失败。这里扫描缺失的 @rpath 依赖并从解释器的 lib 目录补进来。
RESLIB="dist/LaterQueue.app/Contents/Resources/lib"
FW="dist/LaterQueue.app/Contents/Frameworks"
PYLIB=$("$PY" -c 'import sys,os;print(os.path.join(sys.base_prefix,"lib"))' 2>/dev/null)
if [ -d "$RESLIB" ] && [ -d "$PYLIB" ]; then
  added=0
  # 反复扫描：新补进来的库可能又引入新的 @rpath 依赖，直到不再增长
  while : ; do
    round=0
    for so in $(command find "$RESLIB" -name "*.so" -o -name "*.dylib" 2>/dev/null); do
      for dep in $(otool -L "$so" 2>/dev/null | awk '/@rpath/{print $1}'); do
        base=$(basename "$dep")
        { [ -f "$RESLIB/$base" ] || [ -f "$FW/$base" ]; } && continue
        src=$(command find "$PYLIB" -maxdepth 1 -name "$base" 2>/dev/null | head -1)
        if [ -n "$src" ]; then
          cp -L "$src" "$RESLIB/$base" && added=$((added+1)) && round=$((round+1))
        fi
      done
    done
    [ "$round" -eq 0 ] && break
  done
  echo "   补入 $added 个依赖库（大小：$(du -sh dist/LaterQueue.app | cut -f1)）"
fi

echo ""
echo "✅ 完成！应用在 dist/LaterQueue.app"
echo "   把它拖进「应用程序」即可。首次打开若提示身份不明，右键 → 打开。"
echo "   （在 Finder 中查看：open dist/）"
