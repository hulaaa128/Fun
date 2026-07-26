# 用沙漏小精灵生成 macOS App 图标（圆角方形背景 + 居中小精灵）
from PIL import Image, ImageDraw
import os

HERE = os.path.dirname(os.path.abspath(__file__))
pet = Image.open(os.path.join(HERE, "assets", "pet.png")).convert("RGBA")

SIZE = 1024
SS = 2
W = SIZE * SS
img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角方形背景（macOS 图标风格），柔和奶油渐变
top = (250, 240, 222); bot = (243, 226, 196)
bg = Image.new("RGBA", (W, W), (0, 0, 0, 0))
for y in range(W):
    t = y / W
    r = int(top[0]*(1-t)+bot[0]*t); g = int(top[1]*(1-t)+bot[1]*t); b = int(top[2]*(1-t)+bot[2]*t)
    ImageDraw.Draw(bg).line([(0, y), (W, y)], fill=(r, g, b, 255))
# 圆角遮罩
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, W-1, W-1], radius=int(W*0.22), fill=255)
img.paste(bg, (0, 0), mask)

# 居中放小精灵，占约 72%
pw = int(W * 0.72)
ph = int(pw * pet.height / pet.width)
petr = pet.resize((pw, ph), Image.LANCZOS)
img.alpha_composite(petr, ((W - pw)//2, (W - ph)//2 - int(W*0.02)))

icon = img.resize((SIZE, SIZE), Image.LANCZOS)
icon.save(os.path.join(HERE, "assets", "AppIcon.png"))
# 生成多尺寸 .icns
sizes = [16, 32, 64, 128, 256, 512, 1024]
icon.save(os.path.join(HERE, "assets", "AppIcon.icns"),
          format="ICNS", sizes=[(s, s) for s in sizes])
print("saved AppIcon.png / AppIcon.icns")
