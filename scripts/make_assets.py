from pathlib import Path
import shutil

workspace = Path(__file__).resolve().parent.parent
root = workspace / 'app'
assets = root / 'assets'
assets.mkdir(exist_ok=True)
for source, destination in (('lcsc3d-icon.png', 'app.png'), ('lcsc3d.ico', 'app.ico')):
    shutil.copyfile(assets / 'branding' / source, assets / destination)
licenses=root/'licenses'
licenses.mkdir(exist_ok=True)
shutil.copyfile(workspace/'LICENSE',licenses/'AGPL-3.0.txt')
shutil.copyfile(licenses/'AGPL-3.0.txt',root/'LICENSE')
print('Assets generated')
