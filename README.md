# LCSC3D issue #13 — AD24 A/B samples

This branch contains only two sample archives for comparing LCSC3D v2.5.0 libraries in Altium Designer. It has no product source code.

本分支只存放两份用于在 Altium Designer 中对比 LCSC3D v2.5.0 库文件的样例压缩包，不包含产品源码。

- [`baseline.zip`](baseline.zip) — unmodified v2.5.0 SchLib and PcbLib for C2040 and C2765186 (no 3D). See the README inside the zip.
- [`variant-v9_pad0.zip`](variant-v9_pad0.zip) — the same two parts as a PcbLib-only variant: official V9 stack parameters added, pad Block 5 set to 0 bytes. There is no SchLib in this zip; use `baseline.zip` for schematic libraries.

Both sets have been confirmed to open in **Altium Designer 26 (26.7.1.11)**. That does **not** rule out a hang in **AD24.10.1**, which is the version reported in [issue #13](https://github.com/Travelerrrrrr/LCSC3D/issues/13).

两套样例均已在 **Altium Designer 26（26.7.1.11）** 上确认可以打开。这**不能**排除 **AD24.10.1**（议题 #13 所述版本）仍会卡死。

Please download the zips from:

- https://github.com/Travelerrrrrr/LCSC3D/raw/samples/issue-13/baseline.zip
- https://github.com/Travelerrrrrr/LCSC3D/raw/samples/issue-13/variant-v9_pad0.zip

## SHA-256 of the zip archives / 压缩包 SHA-256

```
ed244466d278029523eb5c96d47b7f767e8dce5b256ec530ee74bb0d579059a8  baseline.zip
bcec8aef9635cdf40e2ca01972efaac475e5dd497fc1de120497428464993138  variant-v9_pad0.zip
```

## 请报告者按下列步骤在 AD24.10.1 中测试

1. 使用 **Altium Designer 24.10.1**（或你当初卡死时所用的同一版本）。每打开一个文件之前，请先完全退出 Altium，再重新启动，然后一次只打开这一个库文件（不要同时打开工程或其它库）。
2. 先测试 `baseline.zip` 里的 4 个文件，顺序建议为：`C2040.SchLib`、`C2040.PcbLib`、`C2765186.SchLib`、`C2765186.PcbLib`。
3. 再测试 `variant-v9_pad0.zip` 里的 2 个文件：`C2040.PcbLib`、`C2765186.PcbLib`。
4. 每个文件请记录下列三种结果之一：
   - **正常打开**：库编辑器出现，可以看到元件，界面可操作；
   - **卡死**：界面无响应。请至少等待 **2 分钟** 再判断为卡死，并记下大约等待了多久；
   - **报错**：若弹出错误对话框，请把对话框里的**原文**完整抄下来或截图。
5. 请明确说明卡死或报错发生在 **SchLib（原理图库）** 还是 **PcbLib（封装库）**，或两者都有。
6. 若确认卡死，可打开 Windows 任务管理器，找到 Altium 进程（常见名为 `X2`），右键选择「创建转储文件」，把生成的 `.dmp` 一并提供（文件可能较大）。
7. 如能找到 Altium 日志，也请一并附上。日志通常在当前 Windows 用户目录下的 `%LOCALAPPDATA%\Altium\` 中，安装目录名类似 `Altium Designer {安装 GUID}`，常见文件包括 `DXP_Startup.log`、`DXP_SplashScreen.log`。请按修改时间选取与这次打开相关的文件。若找不到，直接说明即可，不必猜测路径。
8. 请同时告知：你自己当初卡死时导出的 **C 编号（LCSC 料号）**，以及所用的 **LCSC3D 版本**（例如 2.5.0，或「关于」里的版本号）。

### 结果如何解读

- **基线（baseline）卡死或报错，但变体（v9_pad0）可以正常打开**：说明问题更可能出在 PcbLib 缺少官方 V9 层叠参数，或焊盘 Block 5 的 596 字节写法；可优先从这两处修。
- **基线和变体都卡或都报错**：原因很可能不是（或不只是）V9 层叠或 Block 5，需要再查其它差异（例如原理图库格式、3D、合并库、或特定料号）。
- **只有 SchLib 卡，PcbLib 正常**：应沿着原理图库那条导出路径排查，而不是只改 PcbLib 的 V9 / Block 5。
- **基线和变体在 AD24.10.1 上都能正常打开**：则议题中的卡死可能与特定料号、是否内嵌 3D、是否为合并库、或其它环境因素有关，请补充第 8 步的 C 编号与版本信息。
