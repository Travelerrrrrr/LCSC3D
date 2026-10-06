# JLC3DViewer — 立创 3D 模型下载器

Windows x64 便携软件，当前版本 1.3.0。输入立创 C 编号，批量下载 STEP、WRL、OBJ 三种 3D 模型。右侧保留 3D 模型、符号、封装预览，无需先下载。器件目录按“器件名_编号”命名，不生成 CSV 文件。

1.3.0 已移除符号和封装导出及其转换后端。完整使用说明、依赖版本与许可证见 [应用说明](work/app/README.md)。

## 目录

- `work/app/`：应用源码、测试、图标、许可证和构建脚本。
- `work/upstream/`：easyeda2kicad 上游 Git 子模块，固定到 v1.0.1（`20c754d0fc1fec94e7fab2112b66ea576e8db369`）。
- `work/package_source.py`、`work/finalize_delivery.py`：源码打包和交付验证脚本。
- `outputs/`：交付说明、界面截图和校验和。EXE、ZIP 不纳入 Git。

虚拟环境、构建产物、验证输出、日志和个人设置由 `.gitignore` 排除。

## 开发与构建

在 Windows x64 安装 Python 3.12 后，取得固定版本上游源码并构建：

```powershell
git submodule update --init
cd work/app
.\build.ps1
```

构建脚本会安装 Python 依赖、运行测试并生成 `dist/LCSC3D-Portable-v1.3.0.exe`。无需 C++、Qt 开发包或 EDA 软件。

安装 `work/app/requirements.txt` 后可运行 `python main.py`；本地测试为 `python -m unittest discover -s tests -v`。
