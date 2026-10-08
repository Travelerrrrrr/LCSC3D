# Third-party software notices

This application is distributed under AGPL-3.0-or-later. Complete application source and rebuild scripts are delivered in LCSC3D.zip.

- **Python 3.12.10** — Python Software Foundation and contributors; PSF license. `Python-LICENSE.txt`. Source: https://www.python.org/downloads/release/python-31210/
- **PySide6 / Shiboken / Qt 6.11.1** — The Qt Company and contributors. Open-source LGPL-3.0 and/or GPL-3.0 options apply to the shipped open-source components; see `LGPL-3.0.txt` and `GPL-3.0.txt`, module-specific notices and upstream source. https://code.qt.io/cgit/pyside/pyside-setup.git/ and https://code.qt.io/cgit/qt/
- **Qt WebEngine / Chromium** — contains third-party components under their respective licenses. Official attribution/license page: https://doc.qt.io/qt-6/qtwebengine-licensing.html ; sources: https://code.qt.io/cgit/qt/qtwebengine.git/ and https://chromium.googlesource.com/chromium/src/ . Chromium credits and notices are embedded in the shipped QtWebEngine resources (chrome://credits).
- **PyInstaller 6.20.0 bootloader** — GPL with the bootloader distribution exception; `PyInstaller-COPYING.txt`. https://github.com/pyinstaller/pyinstaller
- **certifi 2026.7.22** — Mozilla Public License 2.0; `certifi-LICENSE.txt`. https://github.com/certifi/python-certifi
- **urllib3 2.7.0** — Andrey Petrov and contributors; MIT. `urllib3-LICENSE.txt`. https://github.com/urllib3/urllib3
- **gmalg 1.1.2** — dongmanyu and contributors; MIT. `gmalg-MIT.txt`. Used for the official CAS SM2 C1C3C2 login encryption, with secure random numbers from Python `secrets`. https://github.com/ww-rm/gmalg
- **OpenSSL**, as included with Python — OpenSSL licenses applicable to the Python runtime distribution. https://www.openssl.org/source/

Qt libraries remain dynamically linked DLLs in the extracted temporary runtime. The provided build script can rebuild the application with modified compatible Qt/PySide libraries.

LCSC3D 2.0.0 does not include or depend on easyeda2kicad or the storefront's 3D engine. Component association, downloads, OBJ reading, WebGL rendering and update handling are implemented in this repository. Foundational Python/Qt/network packages listed above remain under their respective licenses.

Native AD record framing and field layouts were researched against **OriginalCircuit.Altium / AltiumSharp**, commit `ce72437f30cd54f549601d4e0ca5846d21272150`, licensed under Apache-2.0 (see `AltiumSharp-APACHE-2.0.txt`). Source: https://github.com/issus/AltiumSharp . The Python encoder is implemented here and uses Windows structured storage; AltiumSharp, OpenMcdf and .NET are not bundled or runtime dependencies. The independent AltiumSharp 1.0.2 reader is used only for development verification. `olefile` (BSD, https://github.com/decalage2/olefile) is a test-only dependency and is not imported by the application.

Symbols and footprints use official component JSON and SVG data from `https://lceda.cn/api/products/{code}/components` and `/svgs`, with the equivalent official `easyeda.com` APIs as fallback. STEP and OBJ model files are exported unchanged; SchLib/PcbLib are generated from structural component data. JSON and SVG exports, including model-info.json, have been removed. The local SVG canvas and WebGL navigation code belong to this application. Test fixtures include official C2040, C20197, C163691 and C2765186 samples. Attribution to the JLCEDA/EasyEDA Official Library and both official homepage links remain visible in the application, documentation and generated library parameters.
