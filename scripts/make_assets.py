from pathlib import Path
from PIL import Image, ImageDraw
import shutil

workspace = Path(__file__).resolve().parent.parent
root = workspace / 'app'
assets = root / 'assets'
assets.mkdir(exist_ok=True)
scale=4
image=Image.new('RGBA',(256*scale,256*scale),(0,0,0,0))
draw=ImageDraw.Draw(image)
def pts(values):
    return [tuple(v*scale for v in pair) for pair in values]
draw.rounded_rectangle((8*scale,8*scale,248*scale,248*scale),radius=54*scale,fill='#168878')
draw.polygon(pts([(128,54),(193,91),(128,129),(63,91)]),fill='#d0eee6')
draw.polygon(pts([(63,96),(124,132),(124,203),(63,167)]),fill='#fffdf6')
draw.polygon(pts([(132,132),(193,96),(193,167),(132,203)]),fill='#88c7b4')
draw.line(pts([(128,128),(128,206)]),fill='#168878',width=5*scale)
image=image.resize((256,256),Image.Resampling.LANCZOS)
image.save(assets/'app.png')
image.save(assets/'app.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
licenses=root/'licenses'
licenses.mkdir(exist_ok=True)
shutil.copyfile(workspace/'upstream'/'LICENSE',licenses/'AGPL-3.0.txt')
shutil.copyfile(licenses/'AGPL-3.0.txt',root/'LICENSE')
print('Assets generated')
