# 贡献与发行

感谢提交问题、改进和修复。提交问题时请提供软件版本、Windows 版本、器件 C 编号、复现步骤和错误提示。日志及截图请先移除个人目录和敏感信息。

## 开发环境

使用 Windows x64 和 Python 3.12。普通克隆即可，2.0.0 不再使用上游子模块或转换器。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r app/requirements-test.txt
.\.venv\Scripts\python.exe app/main.py
.\.venv\Scripts\python.exe -m unittest discover -s app/tests -v
```

窗口和 WebGL 回归测试需要 Windows；其他平台会跳过原生窗口测试。测试使用本地数据和受控网络服务，不需要真实商城请求。正式成品的在线验证另行执行。

AD 导出回归也需要 Windows，以 `olefile` 独立读取 CFB 并核对引脚、焊盘、槽孔、偏移、来源和关联记录，并用官方 AD 样本核对符号折线坐标、比例与引脚接合。构建仅依赖生产模块，测试读取器不进入 EXE。`LCSC3D.exe --self-test-ad <目录>` 验证冻结程序的实际 AD 导出与界面选项；可追加 `--self-test-ad-parts C20197,C2765186` 指定多个器件。追加 `--self-test-ad-merge` 验证两个合并开关和自定义名称；交付时用 `finalize_delivery.py --merge-dir <该验证目录>` 独立读取合并库并核对条目和引用。仅检查数量或成功解析不能证明几何正确，需检查实际坐标和渲染。

代码变更应保留取消请求、旧响应过滤、有界缓存和原子文件写入。下载列的勾选与用于预览的当前行互相独立。新增测试应验证用户可见行为，避免访问真实远程服务。

不要提交个人设置、日志、Python 环境、构建文件或下载的模型。官方测试样本应保留来源声明，第三方代码及资源应保留相应许可证。

## 构建与验证

运行 `app/build.ps1`，结果为 `app/dist/LCSC3D.exe`。本地交付准备如下：

```powershell
New-Item -ItemType Directory -Path outputs -Force
Copy-Item -LiteralPath app/dist/LCSC3D.exe -Destination outputs/LCSC3D.exe
python scripts/verify_portable.py
python scripts/verify_self_update.py
python scripts/finalize_delivery.py --test-count 322
```

`verify_portable.py` 从独立中文目录运行 EXE，清理 Python/Qt 环境变量并限制 PATH，验证下载前型号、勾选过滤、STEP/OBJ、符号/封装及本地 3D 预览与窗口稳定性，并确认下载目录没有 JSON/SVG 或其他非模型文件。它只重建 `work/便携验证/验证结果/` 中的生成数据，保留正式 EXE；验证结束会删除临时 EXE 副本。

`verify_self_update.py` 使用隔离目录内的真实 EXE 副本验证等待退出、替换、重启确认及设置保留，只关闭本次验证启动的进程。配置和更新暂存位于隔离的 LOCALAPPDATA，额外验证 EXE 目录没有配置和更新子目录，并报告是否跨盘更新。更新 API、校验失败、取消和启动失败恢复由本地测试覆盖。

`verify_app_data.py --output-dir <成品目录> --verify-dir <验证目录>` 使用真实 EXE 和独立 Windows 用户数据目录，普通启动后只关闭本次复制的程序窗口，验证旧配置迁移、新用户默认配置、AppData 日志和运行时解压，以及 EXE 目录没有新增文件。原有用户文件必须保留。

不发布 GitHub 也可以测试完整更新窗口：运行 `python scripts/local_update_test.py`，在自动打开的隔离副本中点击“设置 → 检查更新”→“下载并重启”。追加 `--auto` 自动点击并核对结果；`--scenario bad-checksum` 和 `--scenario no-update` 分别测试校验失败与没有新版。仅启动本机服务，不读取真实会话，不覆盖原始 EXE。完整入口、文件位置和自选候选包见 [本地更新测试](docs/本地更新测试.md)。该脚本只依赖标准库；测试服务仅绑定数字回环地址，不能扩大到局域网或外部更新源。

完整源码包为 `outputs/LCSC3D.zip`，包含应用、构建脚本、文档和许可证。`finalize_delivery.py` 核对成品、更新记录后重新打包源码，逐文件核对 ZIP，比较 EXE 内模块与工作区并生成 SHA-256。上例的测试数量需与实际测试结果一致。可用 `--output-dir`、`--portable-dir`、`--update-report` 和 `--store-dir` 指向独立发行准备目录；本地设置和离线商城记录可分别通过 `--settings-dir` 和 `--store-offline-dir` 提供，`--verification-date` 指定验证日期。即使使用版本子目录，也必须在任务结束前将验证后的最新 EXE、源码 ZIP、使用说明和校验和同步到 `outputs/` 根目录。每次有改动的任务完成后本地提交 Git，详见 [AGENTS.md](AGENTS.md)。

`LCSC3D.exe --capture-docs <目录>` 从实际程序抓取主窗口、设置、符号、封装、商城搜索、完整详情、商品原图与空白登录表单。此模式只访问公开商品资料，不读取用户保存会话，不发送短信或账号登录请求，不保存个人设置；完成后输出截图和验证报告。

`LCSC3D.exe --self-test-settings <目录>` 在隔离目录验证实际 EXE 的设置入口、两个默认系统代理选项、Debug 默认等级、独立保存与恢复、取消保留及日志等级过滤，并实际打包 ZIP、核对诊断内容、取消/确认清除和验证清除后继续记录，输出主窗口和设置截图。须提供隔离的 `LOCALAPPDATA`，它不访问用户保存会话或写入程序旁的正式配置。代理是否真正生效另由本地 HTTP 代理服务回归验证，不只检查下拉框文本。

收藏导入 Demo 可在独立目录构建，避免覆盖正式版：

```powershell
python -m PyInstaller --noconfirm --clean --distpath outputs/demo-2.1.0-demo.5 --workpath work/store-demo5-build app/LCSC3D.spec
```

以上命令从仓库根目录运行。

源码打包使用 `python scripts/package_source.py --output-dir outputs/demo-2.1.0-demo.5`，默认不传参数时仍打包到 `outputs/`。

`LCSC3D.exe --self-test-store <目录>` 使用本地 HTTP 服务验证统一商城入口、独立原生窗口与预览后主窗口进入前台、默认不勾选、50 条分页与跨页勾选、详情和原图画廊、三种原生登录及图片验证、账号收藏添加和取消、收藏导入，并验证 DPAPI 加密、重建窗口后的自动恢复及退出清除，输出 JSON 和截图。兼容旧参数 `--self-test-favorites`。开发时执行 `python app/main.py --self-test-store work/store-smoke` 可运行相同流程。验证时必须同时检查进程退出码与报告中的 `success`。

此项验证的会话文件位于给定的隔离目录，验证结束后清除，不接触用户真实会话。测试不联网、不使用真实账号；真实扫码及收藏读取另行联调。不得把用户的 Cookie、授权码、账号资料或会话文件放入源码、日志及交付包。

Demo.5 的商城离线验证还覆盖单价精度、库存与缺失值、最低起订量、独立梯度与翻页恢复、长文本换行和文字详情滚动，以及删除勾选器件后保留文件和未勾选的下载结果。实网验证额外查询 C499531，检查价格下拉框、库存及「功能特性」的完整内容。实网商城截图必须隐藏账号标签，报告不得包含账号身份。

`--self-test-store-live <目录>` 使用用户已授权且加密保存的会话，只读验证实际 EXE 的自动恢复、真实收藏与勾选导入、C2040 搜索详情、C20618009 完整商城资料、C5879483 三张原图及放大，以及「单片机」前两页、总数与跨页勾选。无可用会话时失败，不自动发起扫码；报告不包含身份、Cookie 或授权码。仅在获准使用该账号联调时执行，不清除用户的已保存登录。实际账号收藏写入另行验证，只添加并取消临时测试收藏，核对原有收藏完整保留。

## 发行

日志改动需运行 `test_logging_diagnostics.py` 的故障注入回归，核对事件是否包含元件/格式/阶段、异常原因链与代码位置，确认后台线程关联、更新助手关联和敏感信息排除。功能事件及排查方法见 [日志覆盖与排查](docs/日志排查.md)。

更新源改动需运行 `test_updater.py` 与 `test_local_update.py`，核对正式源约束、本机 HTTP 下载、代理绕过、跨源重定向拒绝、完整性校验和回滚参数，再执行 `local_update_test.py --auto` 的真实 EXE 验证。`finalize_delivery.py --local-update-report <verification.json>` 将该报告纳入交付核对。

启动后台检查另运行 `test_startup_update.py`，覆盖有新版弹窗、无更新/网络失败静默、只检查一次、手动查询接管和关闭时不等待网络。真实 EXE 使用 `local_update_test.py --auto --startup`，再分别追加 `--scenario no-update`、`--scenario check-failure` 验证静默分支。其他自测入口不自动请求 GitHub。

版本信息在 `app/main.py` 的 `VERSION` 与 `app/version_info.txt` 的 Windows 版本资源中。同步使用说明与版本记录后，通过测试、构建和成品验证，再创建对应的 `vX.Y.Z` Git 标签和 GitHub Release。

Release 上传固定文件名 `LCSC3D.exe`、`LCSC3D.zip` 和 `SHA256SUMS.txt`。EXE、ZIP 与验证输出不放入 Git。修改许可证、上游版本或打包依赖时同步更新第三方声明。

也可在 Actions 中运行 `Prepare release`，填写版本标签和已成功完成的 `Windows build` 运行编号。流程核对应用源码与该构建一致，下载其 EXE，打包完整源码，生成校验和并上传到 Release 草稿；公开发布前核对附件。它不会覆盖已公开的 Release。
