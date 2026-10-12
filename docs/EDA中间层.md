# EDA 中间层与适配器

本地开发版 **2.4.0-demo.1** 使用 IR **1.2**，新增模型资源、放置实例与经过核对的变换矩阵；继续读取原 1.0/1.1 文档。正式版本 2.3.0 使用 IR 1.1。以下保留开发阶段与兼容契约的详细记录。

首次交付：2.3.0-demo.1，IR 协议 1.0。

2.3.0-demo.2 补齐引脚电气类型契约、目标配置和独立的符号/封装合并。IR 继续使用 1.0；旧字段、缺省值和序列化格式保持兼容。

2.3.0-demo.3 修正来源引脚端点识别，补齐 KiCad 合并配置与独立副本，并支持同名库整体替换。IR 仍为 1.0，不改变已发布字段含义。

2.3.0-demo.4 的新数据使用 IR **1.1**：增加独立的 Via 过孔对象、可选来源图元类型和结构化诊断字段。原 1.0 字段和含义保持不变，历史文档仍可读取与往返；不把新增过孔伪装为旧焊盘或普通图形。

## 数据流与边界

官方 JSON → `easyeda_import.parse_component()` → `eda_ir.Component` → 目标能力检查 → AD / KiCad 库。

- `easyeda_import.py` 唯一负责 EasyEDA Standard 的分隔字符串、图层编号、单位和历史字段兼容。引脚以来源电气连接点判定 connection，路径另一端为 body，不假定路径起点一定是连接端；坐标矛盾时报告错误。符号与封装分别解析、分别报告错误；没有符号不阻止有效封装导出。
- `eda_ir.py` 是不可变的元件模型与安全 JSON 编解码器。坐标采用 Decimal 毫米、各单元/封装的局部原点、Y 轴向上；角度为逆时针度数。引脚连接端与本体端分开保存，铜中心与孔中心分开保存，编号使用字符串，允许同号多焊盘。
- `eda_targets.py` 声明目标的软件/文件版本、必要能力、图元与焊盘支持集合。增加 IR 能力不会自动赋予旧导出器支持能力。
- `altium_ir.py`、`kicad.py` 只读取 IR 的语义字段；原始记录扩展不参与目标生成。AD 的二进制封装及已有库管理继续由原模块处理。
- `kicad_export.py` 规划各领域库名、独立副本、保守的封装去重及引用。`library_files.py` 在应用数据目录暂存新文件与原库恢复副本，先提交封装，最后提交符号；同名符号库和整个 `.pretty` 按本批次内容替换，清除旧成员。失败或取消时恢复原库；检测到外部修改时停止覆盖，无法回滚时保留恢复副本并报告位置。其他库不参与替换。底层仍保留默认的排他新建调用，以兼容已有内部调用；导出器显式启用替换。

模型下载与封装关联分别选择。`model_export.py` 获取 STEP/OBJ，核对几何基准后为放置实例生成矩阵和资源摘要；文件内容通过摘要索引单独传递，不塞入 IR JSON。`model_geometry.py` 在隔离、可取消的 CAD 工作进程中核对实体。导出器只接收 IR 和已校验资源，分别生成 AD 原生 3D Body、KiCad 内嵌或外部模型。详见 [3D 模型放置](3D模型放置.md)。

## IR 数据范围

| 类型 | 内容 |
| --- | --- |
| Component | 供应商编号、标题、描述、来源、可选符号/封装、3D 关联和诊断 |
| Symbol / SymbolUnit | 引用前缀、参数、多单元及有序图元 |
| Pin | 名称、字符串编号、电气类型、连接端/本体端、可见性、反相/时钟标记、独立文字属性与位置 |
| Graphic | 矩形及圆角、圆/椭圆、折线、多边形、文字、区域、规范化 SVG 路径、图层和样式 |
| Pad | 铜形状、铜/孔中心、尺寸、轮廓、旋转、孔径、槽长及孔方向、镀孔属性、阻焊/锡膏扩展、机械孔标记 |
| Via | 独立过孔：中心、铜环直径、孔径、起止铜层、来源网络名；本身没有器件引脚编号 |
| Model3D | 资源标识、名称、ModelPlacement、资源摘要引用和 4×4 放置矩阵；空矩阵表示未解析，不能当作单位矩阵 |
| ModelPlacement | 局部毫米位置、X/Y/Z 角度、未旋转 XY 尺寸与明确的 Z→X→Y 统一缩放约定 |
| ModelAsset | 实际 STEP 资源编号、内容摘要、毫米边界、比对 OBJ 摘要及缩放副本的原 STEP 摘要 |
| Extension / Diagnostic | 有命名空间和版本的扩展、是否必要，以及按领域分类的诊断 |

曲线在 IR 中保留路径定义；支持轮廓恢复的多边形焊盘保留原轮廓。`org.easyeda.standard.source-record` 扩展保留来源图元、原点和单位，以便以后迁移尚未建模的属性。它不包含登录会话或接口顶层账号信息。未知目标图元或必要扩展会显式失败，不转换成普通图形掩盖电气语义。

### 过孔与诊断

来源 `VIA` 的铜环字段为直径，孔字段为半径；导入后统一保存毫米直径。当前支持贯穿顶层到底层的圆形镀铜孔，并检查孔径严格小于铜环直径；其他跨层组合明确拒绝。目标按 Via 对象推导必要能力 `through-vias`，未声明此能力的适配器不能接收过孔，且不影响其单独导出符号。

AD 使用原生 VIA 二进制图元；KiCad 封装库不允许板级 via 对象，按其原生 Altium 导入器的方式输出无编号镀通孔，并提示转换规则。保留铜环、孔径和全铜层，不根据位置或网络标签猜测引脚编号；来源网络名留在 IR，库放入 PCB 后由工程确认网络关系。参考：[EasyEDA VIA](https://docs.easyeda.com/en/DocumentFormat/3-EasyEDA-PCB-File-Format/#via)、[KiCad 原生 Altium 图元读取](https://docs.kicad.org/doxygen/altium__parser__pcb_8cpp_source.html)、[KiCad 封装过孔转换](https://docs.kicad.org/doxygen/altium__pcb_8cpp_source.html)。

`Graphic.source_kind` 可选记录来源图元标识，仅用于诊断。未知图元继续保留原始扩展并拒绝导出；错误显示具体类型。`Diagnostic` 的可选 primitive 和来源对象标识随 IR 往返。结构化异常向日志提供固定错误码、领域、图元类型和对象标识，不写入原始记录或异常正文。

### 引脚电气类型

`Pin.electrical` 记录源引脚的电气语义，缺失时为 `unspecified`。词汇包含 input、output、bidirectional、tri_state、passive、free、power、power_in、power_out、open_collector、open_emitter、no_connect、unspecified；是可扩展字符串，未来或来源专用值仍原样往返，不按 KiCad 枚举限制 IR。

EasyEDA Standard 的 0/1/2/3/4 分别为未指定/输入/输出/双向/电源。4 没有声明方向，本版新导入时记录为 `power`；未知代码记为 `source_type_原值`，原始代码同时保存在来源扩展。这修正首版导入器将通用电源推定为 power_in 的行为；旧 IR 中已有的 power_in 仍按电源输入读取，不重新解释或改写。[EasyEDA 官方格式](https://docs.easyeda.com/en/DocumentFormat/2-EasyEDA-Schematic-File-Format/)

KiCad 导出默认逐引脚映射，未知值、空值和缺少方向的 power 输出为 `unspecified`。用户可选择一个受支持类型统一覆盖本批次引脚，覆盖仅作用于导出层，不回写 IR。旧代码接口显式指定 passive 仍可用；AD 保持既有 Passive 策略。矩形直角和填充是 KiCad 目标外观规则：原生 `rectangle`、`fill/type background`，不修改源 `Graphic.radius` 或 `Graphic.fill`，不改变焊盘形状。[KiCad 类型和填充语法](https://dev-docs.kicad.org/en/file-formats/sexpr-intro/)

## 版本和向后兼容

序列化入口为 `dumps(component)` / `loads(text)`，默认只在内存中使用，不自动生成中间文件。

- 主版本表示不兼容的语义变化，次版本表示保持既有行为的扩展。目前只读取主版本 1；遇到主版本 2 会报错。
- 新增可选字段必须提供兼容默认值。已发布字段的含义、单位、方向和编号类型不可原地修改。
- 读取 1.x 的未知可选字段时保留其原始 JSON，并在再次保存时恢复。未知字段不能暗中改变既有字段的含义。
- 需要额外语义的文档必须使用 `required_features` 或 `Extension.required` 声明。程序或目标不支持时拒绝导出。
- 扩展的 payload 是 JSON 文本，不执行任何代码；反序列化仅允许显式注册的类型，并验证字段类型、数值范围及文档大小。
- 当前读取 1.0、1.1 与 1.2，新增可选字段通过默认值读取，未知可选字段继续透传。旧程序遇到 Via / ModelAsset 等未知对象或 `model-placement` 必要能力必须拒绝，不能悄悄丢弃内容。将来引入不兼容版本时须增加明确迁移函数及历史样本测试，不能把未知主版本自动当作当前版本。

历史最小样本 `app/tests/fixtures/ir_v1_0.json` 固定保留；1.1 新增 `ir_v1_1_via.json`。测试覆盖缺失可选字段、未来次版本字段透传、未知主版本、必要能力、嵌套扩展以及原始图元保留。新版本不能修改旧样本来掩盖兼容问题。

## 当前目标能力矩阵

| 能力 | AD | KiCad 8+ |
| --- | --- | --- |
| 多单元、字符串引脚编号、可见性 | 支持 | 支持 |
| 源电气类型 | IR 保留；导出沿用全部 Passive | 默认跟随源类型，可统一覆盖；不支持的值输出 unspecified |
| 字体和配色 | 保留既有嘉立创/AD 风格规则 | 原生字体和默认配色；引脚文字最高 1.27 mm，普通文字保留高度 |
| 圆弧、曲线、圆角、椭圆 | 保持现有原生/折线表达 | 矩形取直角；其他能直接表达的使用原生图元，其余按 0.00127 mm 公差离散 |
| 自定义焊盘、偏心铜、旋转槽孔 | 支持 | 支持；必要时使用有电气锚点的自定义焊盘 |
| 封装内贯穿过孔 | 原生 VIA，参与去重与追加 | 无编号镀通孔，保留几何并提示转换 |
| 铜层挖空 / 板孔区域 | 保持原有区域表达 | 铜层挖空为仅限制铺铜的规则区域，板孔区域为闭合 Edge.Cuts |
| 元件外形投影层 | 保持原 AD 图层与区域 | 以装配/文档轮廓显示内部与外部边界，结果中提示 |
| 独立库、批量合并、保留单器件副本 | 支持 | 符号与封装分别命名、控制合并和副本；库内复用共享封装，先确定名称再写引用；3D 目录与 AD 同规则 |
| 普通导出 / 合并的同名库处理 | 本次内容替换原库 | 本次内容替换原库，包括 `.pretty` 旧成员 |
| 追加已有库 / 工程注册 | 保持现有功能 | 尚未支持 |
| 封装自动绑定 3D | 原生内嵌 STEP，合并/追加均保留资源 | 9/10 内嵌；8 使用库内相对路径 STEP；复制整个 `.pretty` 可搬迁 |

未知图层、无孔全铜层焊盘、单面钻孔焊盘及无法确定的区域映射显式失败。近似转换提示会进入结果文字与应用日志。符号或封装其中一项失败时，可独立完成另一项。

电气类型按用户要求允许退回未指定，与未知关键几何的拒绝策略分别处理。`Options.kicad_merge`、`kicad_library_name` 保留旧调用契约；新调用以 `kicad_merge_symbols`、`kicad_merge_footprints` 独立选择，用 `kicad_symbol_name`、`kicad_footprint_name` 覆盖各领域名称，并以 `keep_kicad_symbols`、`keep_kicad_footprints` 保留副本。老配置只有 kicad_merge 时，两项都继承其值，旧统一库名迁移到两项名称。UI 的 export_target 仅决定本次格式与可见配置，切换目标保留另一目标的设置；AD/KiCad 分别记住合并状态，KiCad 不提供追加模式。

## 增加新适配器

1. 用真实样本明确新软件需要的电气和几何语义，优先复用已有 IR。
2. 需要通用字段时按兼容规则扩充 IR；厂商专用字段使用命名空间扩展。
3. 增加目标能力声明和读取 IR 的序列化器，禁止复制 EasyEDA 字符串解析逻辑。
4. 单独实现目标库的命名、打包、追加及工程注册策略。
5. 运行历史 IR、现有 AD/KiCad 回归，并使用目标软件实际读取及回写验证。

KiCad 参考：[符号格式](https://dev-docs.kicad.org/en/file-formats/sexpr-symbol-lib/)、[封装格式](https://dev-docs.kicad.org/en/file-formats/sexpr-footprint/)、[共用语法](https://dev-docs.kicad.org/en/file-formats/sexpr-intro/)、[CLI](https://docs.kicad.org/9.0/en/cli/cli.html)。
