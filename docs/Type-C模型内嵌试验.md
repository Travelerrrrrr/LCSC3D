# Type-C 模型内嵌 PcbLib 试验

日期：2026-10-12。对象：C2765186，TYPE-C 16PIN 2MD(073)。

本次只验证一个真实 Type-C 样本的原点、高度与原生模型存储。实验入口为 `scripts/try_typec_embedded_3d.py`，未接入正式应用的下载界面、批量合并或追加。没有修改 `app/`，正式 `outputs/LCSC3D.exe` 保持原有功能。

## 数据与放置

封装使用仓库中的公开官方快照 `app/tests/fixtures/C2765186.json`。关联 STEP 资源编号为 `4ee8413127e64716b804db03d4b340ae`；使用已有的官方下载验证样本，不访问用户登录会话。STEP SHA-256：

`a8e4d6ea32a2670892ed935f7bba74bfe9acd9ba3f057db08bad17326362c534`

OpenCascade 读取完整 STEP 实体及其装配变换，得到原始范围：

| 轴 | 最小值 / mm | 最大值 / mm |
| --- | ---: | ---: |
| X | -4.470030479 | 4.470030479 |
| Y | -3.665000100 | 3.915000000 |
| Z | -2.580000000 | 1.581000100 |

STEP 的 XY 投影中心为 `(0, 0.12499995)` mm，并非原始 STEP 原点。官方 `c_origin` 与封装 `head.x/y` 的差换算到局部毫米坐标后为 `(0, -1.1999976)`；官方 Z 字段为 `-0.8999982` mm。源数据的旋转为零、缩放为一。

本样本按“先将资源的 XY 投影中心与最低 Z 归一化，再应用官方偏移”的放置候选进行验证：

```text
Tx = source_x - (STEP xmin + STEP xmax) / 2
Ty = source_y - (STEP ymin + STEP ymax) / 2
Tz = source_z - STEP zmin
```

得到最终刚体位移：

| 参数 | 值 |
| --- | ---: |
| X 位移 | 0 mm |
| Y 位移 | -1.32499755 mm |
| Z 位移 | +1.68000180 mm |
| XYZ 旋转 | 0°、0°、0° |

这里的包围盒计算用于还原这个样本的来源锚点约定，结果另外用真实焊接面和定位柱核验；不把任意模型的包围盒中心当作封装原点，也不把最低点强制放到板面。不得未经验证将此候选算法推广到其他来源、非零旋转或非单位缩放。

## 实体几何核验

- STEP 中识别出 12 个共面的焊接接触面，原始高度均为 `-1.68 mm`。最终位移后高度为约 `+0.0000018 mm`，落在安装板面 `Z=0`；验收容差为 0.005 mm。
- 12 个接触面的 XY 范围全部包含在对应铜焊盘内，包含宽电源端和窄信号端；不只比较整体模型中心。
- 两个定位柱的投影中心分别与两个非镀孔中心相差约 `0.014923 mm`，小于 0.05 mm 的本次样本验收容差。
- 最低结构由原始 `-2.58 mm` 移到 `-0.8999982 mm`，保留约 0.9 mm 的板下插入深度。
- 对照库保留原始 STEP 坐标，可以观察原点和安装面的偏差。对比图由 STEP 实体网格与真实封装焊盘绘制，不是 AD 界面截图。

## 文件与原生检查

输出位于已忽略的 `outputs/typec-embedded-3d/`：

| 文件 | 用途 |
| --- | --- |
| `C2765186-TypeC-3D-AD26.PcbLib` | 已由安装的 AD 26 打开并保存的样例，供实测优先使用 |
| `C2765186-TypeC-3D.PcbLib` | Python 实验编码器直接生成的原生库 |
| `C2765186-TypeC.SchLib` | 配套符号库，引用名称与封装一致 |
| `C2765186-control-original-origin.PcbLib` | 零位移对照库，仅用于观察偏差 |
| `alignment.png` | 原始放置与校正放置的俯视、侧视比较 |
| `verification.json` | 直接编码文件与 STEP 几何的检查结果 |
| `native-ad-verification.json` | AD 原生回存后的独立检查结果 |
| `kicad-import-verification.json` | 对 AD 回存样例的独立导入检查结果 |

Python 编码器通过 Windows CFB 写入一条 3D Body、`Library/Models` 元数据与编号 `0` 的 zlib STEP 流。独立 `olefile` 读取器确认：原有 18 个焊盘/孔和 25 个二维图元保持不变，Body 的模型 ID 能解析到内嵌 STEP，解压数据与原 STEP 逐字节相同；不存在外部模型路径依赖。

AD 26 通过隔离的脚本打开实验库，报告 `components=1` 并成功保存。回存后独立解码确认 44 个图元中有 18 个焊盘/孔和一条 3D Body，STEP 字节及关联 ID 保留，位移为 `(0, -1.32499862, 1.68000172)` mm。与编码器的差异不超过 0.00001 mm。

直接编码库暂用原生模型校验值 `0`；AD 回存将它计算为元数据中的有符号 `-205047408` 和 Body 中的无符号 `4089919888`，两者按 32 位归一化后相同。SHA-256 用于本次资源完整性核验，不能冒充 AD 内部模型校验算法。生产导出前仍需确认该内部校验的独立生成策略。

KiCad 10.0.7 的原生 Altium 导入器另行读取 AD 回存库，识别出 18 个焊盘/孔、1 个内嵌模型、零旋转和 `(0, -1.325, 1.68)` mm 位移。该交叉检查不替代 AD 的三维视图验收。

本次脚本回归共 6 项通过，覆盖固定样本放置、板下结构保留、资源尺寸错误拒绝、非零旋转拒绝、二维几何保留、断开的模型 ID 和 STEP 内容篡改拒绝。既有 `test_altium.py` 的 16 项回归也全部通过，共 22 项。

## 复现

使用开发环境 Python 与独立的 OpenCascade 包目录；这些 CAD 包不进入正式 EXE。例：

```powershell
app/.venv/Scripts/python.exe scripts/try_typec_embedded_3d.py `
  --step work/C48800488-step-investigation/geometry-profile/LCSC3D/sample-step-geometry/C2765186.step `
  --cad-package-dir work/C48800488-step-investigation/geometry-profile/LCSC3D/analysis-packages `
  --profile work/typec-embed/LocalAppData `
  --output outputs/typec-embedded-3d
```

脚本的模型/库输出目录由调用者显式指定。解压模型、配置、日志、Matplotlib 缓存及 AD 脚本暂存位于隔离的 `LOCALAPPDATA/LCSC3D/`；测试 TEMP/TMP 指向 `work/typec-embed/Temp`。外部 AD 的 Roaming 配置另置于同一验证目录的 `RoamingAppData/`，重复运行复用此隔离配置，不读取真实的商城或 AD 登录凭据。

可选 `--altium-exe <已安装的 X2.EXE>` 尝试原生打开/保存。需要启动时已有可用的 AD 状态；隔离配置可能出现 Sign In 窗口而阻止脚本运行。入口拒绝在已有 X2 进程时执行，超时只结束本次创建的进程。几何检查报告会先落盘。本轮已有一次原生成功检查；再次启动的登录状态不保证可复现。

## 尚待验收

在 AD 中用优先样例打开三维视图，实际检查模型外观；将器件放到 PCB 后检查 90° 旋转、翻到底层和整板 STEP 导出。本次完成了原生打开/保存、独立导入和 CAD 几何核验，没有把这些后续操作记为通过。

批量合并、追加、非零旋转、多个模型以及 GUI 配置尚未实现。本实验不改变正式版“需要手动关联 3D”的功能说明。

格式参考：[Altium 3D Body](https://www.altium.com/documentation/altium-designer/pcb/3d-bodies)、[OriginalCircuit.Altium / AltiumSharp](https://github.com/issus/AltiumSharp)、[KiCad Altium 导入器源码](https://docs.kicad.org/doxygen/altium__pcb_8cpp_source.html)。
