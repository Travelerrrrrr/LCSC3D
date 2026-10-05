# Third-party software notices

This application is distributed under AGPL-3.0-or-later. Complete application source and rebuild scripts are delivered in the corresponding LCSC3D-Source archive.

- **easyeda2kicad 1.0.1** — copyright uPesy and contributors; AGPL-3.0. Unmodified upstream source is included in `upstream/`. https://github.com/uPesy/easyeda2kicad.py
- **Python 3.12.10** — Python Software Foundation and contributors; PSF license. `Python-LICENSE.txt`. Source: https://www.python.org/downloads/release/python-31210/
- **PySide6 / Shiboken / Qt 6.11.1** — The Qt Company and contributors. Open-source LGPL-3.0 and/or GPL-3.0 options apply to the shipped open-source components; see `LGPL-3.0.txt` and `GPL-3.0.txt`, module-specific notices and upstream source. https://code.qt.io/cgit/pyside/pyside-setup.git/ and https://code.qt.io/cgit/qt/
- **Qt WebEngine / Chromium** — contains third-party components under their respective licenses. Official attribution/license page: https://doc.qt.io/qt-6/qtwebengine-licensing.html ; sources: https://code.qt.io/cgit/qt/qtwebengine.git/ and https://chromium.googlesource.com/chromium/src/ . Chromium credits and notices are embedded in the shipped QtWebEngine resources (chrome://credits).
- **PyInstaller 6.20.0 bootloader** — GPL with the bootloader distribution exception; `PyInstaller-COPYING.txt`. https://github.com/pyinstaller/pyinstaller
- **certifi 2026.7.22** — Mozilla Public License 2.0; `certifi-LICENSE.txt`. https://github.com/certifi/python-certifi
- **OpenSSL**, as included with Python — OpenSSL licenses applicable to the Python runtime distribution. https://www.openssl.org/source/

Qt libraries remain dynamically linked DLLs in the extracted temporary runtime. The provided build script can rebuild the application with modified compatible Qt/PySide libraries.

Official online viewer scripts are downloaded at runtime from the public URLs used by the LCSC storefront; they are not included in this distribution. Downloaded part data is attributed to JLCEDA/EasyEDA Official Library and retains the upstream copyright/source notice in `model-info.json`.
