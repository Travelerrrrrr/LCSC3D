<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="app/assets/branding/lcsc3d-logo-dark.svg">
    <img src="app/assets/branding/lcsc3d-logo.svg" width="360" alt="LCSC3D">
  </picture>
</p>

**简体中文** | [English](README.en.md)

# LCSC3D：立创元件 3D 模型下载与 AD / KiCad 元件库导出

[![Release](docs/images/release-badge.svg)](https://github.com/Travelerrrrrr/LCSC3D/releases/latest)
[![Windows build](https://github.com/Travelerrrrrr/LCSC3D/actions/workflows/windows.yml/badge.svg)](https://github.com/Travelerrrrrr/LCSC3D/actions/workflows/windows.yml)
[![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011%20x64-0078D6)](https://github.com/Travelerrrrrr/LCSC3D/releases/latest)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![赞助支持 LCSC3D](docs/images/sponsorship/button.svg)](docs/sponsorship.md)

**从立创商城搜索、账号收藏或 C 编号选择元件，预览官方资源，批量下载 STEP / OBJ，并导出 AD 或 KiCad 符号库与封装库。**

LCSC3D 是面向硬件开发、PCB 设计和结构配合的 Windows 便携工具。下载 `LCSC3D.exe` 即可运行，无须安装 Python；生成元件库也无须安装对应的 EDA 软件。

当前源码与本地交付为 **2.4.0-demo.3**，封装模型按原始 STEP 尺寸导出，取消 OBJ 核对与自动缩放。以下在线下载仍指向正式版 2.3.0；本次未发布 Release。

**当前版本：2.3.0** · **[下载 Windows 程序](https://github.com/Travelerrrrrr/LCSC3D/releases/latest/download/LCSC3D.exe)** · [完整源码包](https://github.com/Travelerrrrrr/LCSC3D/releases/latest/download/LCSC3D.zip) · [使用说明](app/README.md) · [本版更新](docs/releases/v2.3.0.md) · [问题反馈](https://github.com/Travelerrrrrr/LCSC3D/issues)

![LCSC3D 2.3.0 工作台：KiCad 符号预览与导出设置](docs/images/2.3.0-release/kicad.png)

## 2.3.0 更新重点

- **新增 KiCad 8+ 导出**：生成 `.kicad_sym` 符号库和 `.pretty` 封装库，支持独立导出、分别合并和保留单器件副本。
- **按软件显示配置**：通过“导出到”切换 AD / KiCad，分别记住设置。KiCad 可配置引脚电气类型，并显示对应的符号预览。
- **引入 EDA 中间层**：统一保存元件的电气与几何信息，由各软件的导出器生成文件，为后续适配其他 EDA 提供基础。
- **修复导出与排版**：修正部分符号的引脚方向，补齐封装过孔，修复长结果文字重叠、控制栏间距和点击“合并”时的闪动。
- **改进选型和文件管理**：搜索与收藏列表增加商品缩略图，搜索每页 20 项；AD 追加支持自定义工程输出目录、库名及按钮直接导入工程。完善导出日志与故障诊断。

旧版变化见 [版本历史](docs/版本历史.md)。

## 开始使用

1. 从 [Releases](https://github.com/Travelerrrrrr/LCSC3D/releases/latest) 下载 `LCSC3D.exe`，在 Windows 10/11 x64 上运行。
2. 点击 **立创商城** 搜索元件，或登录后从 **账号收藏** 勾选并加入下载列表；也可输入 `C2040, C20197, C456013` 后点击 **载入列表**。
3. 单击器件行查看预览，勾选需要导出的器件，选择保存目录。
4. 在 **导出到** 中选择 **Altium Designer / KiCad**，勾选所需模型和元件库格式；需要汇总时启用 **Lib → 合并**，分别设置库名。
5. 点击 **开始下载**，完成后点击 **打开器件目录**。在 AD 中打开 SchLib / PcbLib；在 KiCad 库管理器中添加符号库文件和封装库目录。

**再次合并以本次勾选为准。** AD 和 KiCad 的普通导出、合并会在生成成功后覆盖同名库；KiCad 会替换整个同名 `.pretty` 目录，移除旧成员。需要保留的元件须一并勾选。AD 的 **追加** 模式用于向已有库增加元件，KiCad 暂不提供追加。

## 符号与封装导出

| 功能 | Altium Designer | KiCad 8+ |
| --- | --- | --- |
| 文件格式 | 原生 `.SchLib` / `.PcbLib` | `.kicad_sym` / `.pretty` 内的 `.kicad_mod` |
| 独立与合并 | 符号、封装分别控制合并与库名，可保留单器件副本 | 相同操作方式，共享封装自动复用 |
| 引脚电气类型 | 统一导出为 Passive | 默认跟随源数据，也可统一指定；不支持的类型为“未指定” |
| 符号风格 | 嘉立创 / AD，可在预览页切换并应用到导出 | 固定 KiCad 风格；矩形使用直角、实心主体背景填充 |
| 封装几何 | 焊盘、钻孔、槽孔、铜区、图形及原生过孔 | 焊盘、钻孔、槽孔、铜区和图形；过孔以无编号镀通孔保留 |
| 追加与工程 | 追加已有库、自定义工程目录与库名、导入 `.PrjPcb` | 暂不支持追加和工程自动注册 |

KiCad 添加封装库时，**库昵称应与导出库名一致**，例如 `LCSC3D`，以便符号找到对应封装。KiCad 封装中的过孔保留位置、孔径和铜环，网络归属需在 PCB 中确认。导出过程需要近似转换时，会在结果中提示。

两种目标均支持多单元符号、字符串引脚编号和常见贴片 / 通孔封装。正式版 2.3.0 需要手动关联模型；本地开发版 **2.4.0-demo.3 默认关联 3D**，支持 AD 内嵌、KiCad 9/10 内嵌与 KiCad 8 外部模型，详见 [使用说明](app/README.md)。AD 暂不生成 IntLib。

具体操作见 [使用说明](app/README.md)。

## KiCad 导出效果

以下为用户提供的 KiCad 实测截图，展示导出的符号和封装。点击图片可查看原图。

| 集成电路符号 | MOS 管符号 |
| --- | --- |
| <a href="docs/images/2.3.0-release/kicad-symbol-ic-user.png"><img src="docs/images/2.3.0-release/kicad-symbol-ic-user.png" alt="用户实测：KiCad 中打开集成电路符号，显示引脚与实心主体背景" width="440"></a> | <a href="docs/images/2.3.0-release/kicad-symbol-mosfet-user.png"><img src="docs/images/2.3.0-release/kicad-symbol-mosfet-user.png" alt="用户实测：KiCad 中打开 MOS 管符号，显示引脚编号" width="440"></a> |

| 带过孔的封装 | 自定义焊盘封装 |
| --- | --- |
| <a href="docs/images/2.3.0-release/kicad-footprint-vias-user.png"><img src="docs/images/2.3.0-release/kicad-footprint-vias-user.png" alt="用户实测：KiCad 封装中的九个焊盘与四个过孔" width="440"></a> | <a href="docs/images/2.3.0-release/kicad-footprint-custom-pads-user.png"><img src="docs/images/2.3.0-release/kicad-footprint-custom-pads-user.png" alt="用户实测：KiCad 封装中的自定义焊盘与丝印" width="440"></a> |

**带散热焊盘的封装**

<a href="docs/images/2.3.0-release/kicad-footprint-thermal-pad-user.png"><img src="docs/images/2.3.0-release/kicad-footprint-thermal-pad-user.png" alt="用户实测：KiCad 封装中的十五号散热焊盘和两侧引脚焊盘" width="440"></a>

## AD 导出效果

以下保留此前的 AD 实测截图，展示符号库、封装库和 PCB 工程中的库引用。更多符号、封装及三维示例可展开查看。

| AD 中的原理图库 | AD 中的 PCB 封装库 |
| --- | --- |
| <a href="docs/images/2.2.0/ad-symbol-user.png"><img src="docs/images/2.2.0/ad-symbol-user.png" alt="用户实测：AD 中打开 SchLib 并显示 MOS 管符号与引脚" width="440"></a> | <a href="docs/images/2.2.0/ad-footprint-user.png"><img src="docs/images/2.2.0/ad-footprint-user.png" alt="用户实测：AD 中打开 PcbLib 并显示 20 焊盘封装" width="440"></a> |

<a href="docs/images/2.2.0/ad-project-user.png"><img src="docs/images/2.2.0/ad-project-user.png" alt="用户实测：PCB 工程中已包含 PcbLib 和 SchLib 库引用" width="340"></a>

<details>
<summary>更多既有 AD 实机示例：多单元符号、BGA、连接器和手动添加 STEP 后的三维效果</summary>

以下历史三维截图展示在 AD 中**另外添加 STEP 模型后**的效果，拍摄于本地开发版加入自动模型关联之前。

| 多单元原理图符号 | BGA 封装 |
| --- | --- |
| <a href="docs/images/screenshots/4.png"><img src="docs/images/screenshots/4.png" alt="AD 中打开多单元原理图库" width="440"></a> | <a href="docs/images/screenshots/5.png"><img src="docs/images/screenshots/5.png" alt="AD 中打开 BGA PCB 封装库" width="440"></a> |

| 焊盘与铜区细节 | 异形焊盘示例 |
| --- | --- |
| <a href="docs/images/screenshots/6.png"><img src="docs/images/screenshots/6.png" alt="AD 中查看焊盘与铜区细节" width="440"></a> | <a href="docs/images/screenshots/7.png"><img src="docs/images/screenshots/7.png" alt="AD 中查看异形焊盘和封装铜区" width="440"></a> |

| 连接器二维封装 | 添加 STEP 后的 AD 三维视图 |
| --- | --- |
| <a href="docs/images/screenshots/8.png"><img src="docs/images/screenshots/8.png" alt="AD 中打开连接器封装" width="440"></a> | <a href="docs/images/screenshots/9.png"><img src="docs/images/screenshots/9.png" alt="AD 中添加 STEP 后的三维视图" width="440"></a> |

| 连接器三维视图 | 连接器另一侧视图 |
| --- | --- |
| <a href="docs/images/screenshots/10.png"><img src="docs/images/screenshots/10.png" alt="AD 中添加 STEP 后的连接器三维视图" width="440"></a> | <a href="docs/images/screenshots/11.png"><img src="docs/images/screenshots/11.png" alt="AD 中添加 STEP 后的连接器另一侧视图" width="440"></a> |

| Type-C 三维视图 | Type-C 底部视图 |
| --- | --- |
| <a href="docs/images/screenshots/12.png"><img src="docs/images/screenshots/12.png" alt="AD 中添加 STEP 后的 Type-C 连接器" width="440"></a> | <a href="docs/images/screenshots/13.png"><img src="docs/images/screenshots/13.png" alt="AD 中添加 STEP 后的 Type-C 连接器底部" width="440"></a> |

</details>

## 中间层与导出器架构

```mermaid
flowchart LR
    A[官方元件 JSON] --> B[统一导入器]
    B --> C[版本化 EDA 中间层]
    C --> D[AD 导出器]
    C --> E[KiCad 导出器]
    D --> F[SchLib / PcbLib]
    E --> G[kicad_sym / pretty]
```

中间层集中记录符号与多单元、引脚名称和编号、连接方向与电气类型，以及图形、文字、图层、焊盘、钻孔、槽孔、过孔和 3D 来源关联。AD 与 KiCad 导出器读取同一份数据，再按各软件的格式和配置生成元件库。

中间层采用独立版本和兼容规则：新增可选字段提供默认值，保持已有字段含义，继续读取旧数据。无法可靠转换的关键几何会明确提示，不静默丢弃。后续适配其他 EDA 时，可以增加导出器并复用已有导入与元件信息；各软件的风格和库管理规则由对应导出器处理。

设计与兼容细节见 [EDA 中间层说明](docs/EDA中间层.md)。

## 选型、预览与批量下载

| 功能 | 使用方式 |
| --- | --- |
| 商城与收藏 | 按 C 编号、型号或关键词搜索；每页 20 项，跨页保留勾选。搜索与收藏列表显示缩略图，双击查看原图。 |
| 价格与资料 | 查看库存、独立选择价格梯度，阅读完整参数，打开数据手册和全部原图。 |
| 四类预览 | 在 3D 模型、符号、封装、商品图片间切换；支持缩放、平移和适应窗口。 |
| 批量下载 | STEP / OBJ 与所选目标的两种库格式可自由组合；逐器件显示进度与结果，支持停止和重试。 |
| 合并整理 | 复用相同封装，可保留独立器件；不保留独立副本时，模型集中在 `库名_3D` 文件夹。 |

没有 3D 模型的元件仍可导出可用的符号和封装；某一格式失败时保留其他成功资源。资源是否可用以官方数据和任务结果为准。

## 设置、日志与更新

**设置** 提供简体中文 / English、跟随系统 / 浅色 / 深色、配色、按钮文字颜色、字体和 10–24px 字号。English 使用 LCSC 国际商城，显示英文资料及美元价格；国际站登录与收藏通过 LCSC.com 网页管理，国内账号会话与国际站分别使用。

商城和更新可分别选择系统代理或直连。**日志与诊断** 可选择日志等级、打开目录、打包或清除日志；记录下载、AD / KiCad 转换、文件替换和失败恢复等步骤。诊断包不收集登录凭据，清除后继续记录和捕获崩溃。AD 追加与工程修改支持自动备份，可在 **设置 → 备份** 查看和恢复。

配置、会话、日志、缓存和更新数据统一存放在 `%LOCALAPPDATA%/LCSC3D/`。用户选择的模型和元件库导出目录独立设置。

启动后后台检查正式新版，也可在 **设置 → 关于与支持 → 检查更新** 手动查询。下载并通过 SHA-256 校验后，可选择 **下载并重启**；更新保留设置和已导出资源。Release 提供便携程序 `LCSC3D.exe`、完整源码 `LCSC3D.zip` 和校验文件 `SHA256SUMS.txt`。

## 使用范围

下载和在线预览需要联网，3D 预览需要支持 WebGL 的显卡驱动。已验证 KiCad 8 / 9 / 10；支持情况与本版验证范围见 [验证记录](docs/2.3.0验证记录.md)。

预览和导出用于辅助选型与设计，生产使用前仍需按数据手册核对引脚、电气类型、尺寸和封装。此前用旧版导出的引脚方向异常符号，需要重新导出。

## 源码运行与构建

开发环境为 Windows x64、Python 3.12。

```powershell
git clone https://github.com/Travelerrrrrr/LCSC3D.git
cd LCSC3D
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r app/requirements-test.txt
.\.venv\Scripts\python.exe app/main.py
```

运行 `app/build.ps1` 完成测试与便携程序构建。开发和发行流程见 [贡献指南](CONTRIBUTING.md)。

## 许可证、数据来源与支持

项目代码采用 [AGPL-3.0-or-later](LICENSE)，依赖声明见 [第三方声明](app/licenses/THIRD-PARTY.md)。符号、封装和模型来自 [嘉立创EDA / JLCEDA 官方库](https://lceda.cn/) 与 [EasyEDA 官方库](https://easyeda.com/)；软件许可证不替代元件数据自身的版权与使用条件。

欢迎通过 [Issues](https://github.com/Travelerrrrrr/LCSC3D/issues) 反馈问题，附上 C 编号、版本、所选格式、错误提示与截图。也欢迎点 Star 或 [赞助支持](docs/sponsorship.md)。

## 鸣谢

感谢 [uPesy/easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py) 为 EasyEDA / LCSC 元件到 KiCad 的转换提供思路和参考。
