# Altium 转换后端

`lcsc-altium.exe` 是离线命令行适配器，只读取 Python 下载层取得的 EasyEDA JSON 和 STEP，再调用 EasyKiConverter 的符号、封装导入器和 Altium 写入器。生成文件无需安装 Altium Designer。

`vendor/EasyKiConverter/` 保留未修改的相关源码、GPL-3.0 许可证及 `upstream.json` 中的固定提交和逐文件 SHA-256。未采用 EasyKiConverter 的 GUI、网络层或其发布版运行包。

本地适配器的 normalize.h 将封装 IR 从 EasyEDA 的向下纵轴转换到 AD 的向上纵轴，同时转换图元角度和模型放置坐标。符号 IR 的上游构建器已完成纵轴转换，不重复处理。测试独立回读 PcbLib 焊盘编号、间距、尺寸和旋转以验证转换。

构建需要 CMake、Ninja、MinGW C++17 编译器和 Qt 6 Core/Gui 开发包：

```powershell
python native/build_native.py --qt-prefix C:/Qt/6.x/mingw_64 --compiler-prefix C:/Qt/Tools/mingw/bin
```

生成的 `native/runtime/` 包含转换器和它独立使用的 DLL，打包后随主 EXE 分发；它不使用主界面的 Qt DLL。完整操作参见应用 README。
