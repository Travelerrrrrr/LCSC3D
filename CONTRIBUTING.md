# 贡献与发行

感谢提交问题、改进和修复。提交问题时请提供软件版本、Windows 版本、器件 C 编号、复现步骤和错误提示。日志及截图请先移除个人目录和敏感信息。

## 开发环境

使用 Windows x64 和 Python 3.12。克隆仓库时带上 `--recurse-submodules`；已有克隆执行 `git submodule update --init --recursive`。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r app/requirements.txt
.\.venv\Scripts\python.exe app/main.py
.\.venv\Scripts\python.exe -m unittest discover -s app/tests -v
```

窗口和 WebGL 回归测试需要 Windows；其他平台会跳过原生窗口测试。测试使用本地数据和受控网络服务，不需要真实商城请求。正式成品的在线验证另行执行。

代码变更应保留取消请求、旧响应过滤、有界缓存和原子文件写入。下载列的勾选与用于预览的当前行互相独立。新增测试应验证用户可见行为，避免访问真实远程服务。

不要提交个人设置、日志、Python 环境、构建文件或下载的模型。官方测试样本应保留来源声明，第三方代码及资源应保留相应许可证。

## 构建与验证

运行 `app/build.ps1`，结果为 `app/dist/LCSC3D.exe`。本地交付准备如下：

```powershell
New-Item -ItemType Directory -Path outputs -Force
Copy-Item -LiteralPath app/dist/LCSC3D.exe -Destination outputs/LCSC3D.exe
python scripts/verify_portable.py
python scripts/package_source.py
python scripts/finalize_delivery.py --test-count 64
```

`verify_portable.py` 从独立中文目录运行 EXE，清理 Python/Qt 环境变量并限制 PATH，验证下载前型号、勾选过滤、三种格式、在线预览与窗口稳定性。它只重建 `work/便携验证/验证结果/` 中的生成数据，保留正式 EXE；验证结束会删除临时 EXE 副本。

完整源码包为 `outputs/LCSC3D.zip`，包含应用、构建脚本、文档、许可证和固定版本上游源码。`finalize_delivery.py` 核对原始验证结果，生成交付说明、截图和 SHA-256，并更新 `docs/验证记录.md`。上例的测试数量需与实际测试结果一致。

## 发行

版本信息在 `app/main.py` 的 `VERSION` 中。同步使用说明与版本记录后，通过测试、构建和成品验证，再创建对应的 `vX.Y.Z` Git 标签和 GitHub Release。

Release 上传固定文件名 `LCSC3D.exe`、`LCSC3D.zip` 和 `SHA256SUMS.txt`。EXE、ZIP 与验证输出不放入 Git。修改许可证、上游版本或打包依赖时同步更新第三方声明。
