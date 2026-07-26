# 生成沙漏小精灵 sprite（原创）。改这里就能调整形象。
from PIL import Image, ImageDraw
SS = 3          # 超采样，边缘更平滑
OUT = 2         # 输出放大（更清晰）
W, H = 400, 480

def render(path, blink=False):
    img = Image.new("RGBA", (W*SS, H*SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    def S(v): return v*SS
    def box(x0, y0, x1, y1): return [S(x0), S(y0), S(x1), S(y1)]
    def ell(x0, y0, x1, y1, **k): d.ellipse(box(x0, y0, x1, y1), **k)
    def poly(pts, **k): d.polygon([(S(x), S(y)) for x, y in pts], **k)
    def line(pts, fill, width): d.line([(S(x), S(y)) for x, y in pts], fill=fill, width=int(S(width)))
    def arc(x0, y0, x1, y1, start, end, fill, width): d.arc(box(x0, y0, x1, y1), start, end, fill=fill, width=int(S(width)))

    BODY=(175,201,222,255); BODYO=(120,155,190,255)
    CREAM=(246,238,223,255); WOOD=(176,132,80,255); SAND=(232,163,60,255)
    GLASS=(251,243,226,255); DARK=(52,54,70,255); WHITE=(255,255,255,255)
    CHEEK=(243,166,160,190)

    ell(140,420,200,458,fill=BODY,outline=BODYO,width=S(3))
    ell(200,420,260,458,fill=BODY,outline=BODYO,width=S(3))
    line([(200,150),(200,118)],BODYO,3)
    ell(186,98,214,126,fill=SAND,outline=WOOD,width=S(2))
    ell(46,298,96,356,fill=BODY,outline=BODYO,width=S(3))
    ell(304,298,354,356,fill=BODY,outline=BODYO,width=S(3))
    ell(58,150,342,446,fill=BODY,outline=BODYO,width=S(5))
    # 肚子只画空的奶油色圆底；沙漏改由 laterqueue 运行时绘制（支持漏沙+翻转动画）
    ell(133,258,267,400,fill=CREAM)
    ell(118,232,150,254,fill=CHEEK)
    ell(250,232,282,254,fill=CHEEK)
    if blink:
        # 闭眼：向下弯的笑眼弧线（配合腮红，像开心眯眼）
        arc(150,206,186,240,200,340,DARK,4)
        arc(214,206,250,240,200,340,DARK,4)
    else:
        # 睁眼：黑椭圆 + 高光
        ell(150,198,186,246,fill=DARK)
        ell(214,198,250,246,fill=DARK)
        ell(158,206,172,222,fill=WHITE)
        ell(222,206,236,222,fill=WHITE)
    d.arc(box(186,236,214,260),20,160,fill=DARK,width=S(3))

    img.resize((W*OUT, H*OUT), Image.LANCZOS).save(path)
    print("saved", path)

if __name__ == "__main__":
    import os
    here = os.path.dirname(__file__)
    render(os.path.join(here, "assets", "pet.png"))
    render(os.path.join(here, "assets", "pet_blink.png"), blink=True)
