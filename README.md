# JLC3DViewer — 立创 3D 模型下载器

Windows x64 便携软件，当前版本 1.0.1。输入立创 C 编号，批量下载 STEP、WRL、OBJ 模型，并通过商城官方查看器在线预览。

完整使用说明、依赖版本与许可证信息见 [应用说明](work/app/README.md)。

## 目录

- `work/app/`：应用源码、测试、图标、许可证和构建脚本。
- `work/upstream/`：easyeda2kicad 上游 Git 子模块，固定到 v1.0.1（`20c754d0fc1fec94e7fab2112b66ea576e8db369`）。
- `work/collect_licenses.py`、`work/make_assets.py`：许可证收集和图标生成脚本。
- `work/package_source.py`、`work/finalize_delivery.py`：源码打包和交付整理脚本。
- `outputs/`：交付说明、界面截图和校验和。EXE、ZIP 不纳入 Git。

虚拟环境、构建产物、研究缓存、验证输出、日志和个人设置由 `.gitignore` 排除。

## 开发与构建

从新的仓库副本开始时，先取得固定版本的上游源码：

```powershell
git submodule update --init
```

在 Windows x64、Python 3.12 环境中构建：

```powershell
cd work/app
.\build.ps1
```

构建脚本会创建虚拟环境、安装依赖、运行测试并生成 `dist/LCSC3D-Portable-v1.0.1.exe`。

安装 `work/app/requirements.txt` 后，可在应用目录运行 `python main.py`；本地测试命令为 `python -m unittest discover -s tests -v`。
