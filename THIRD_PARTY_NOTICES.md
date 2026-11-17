# 第三方组件与数据范围

仓库的 `LICENSE` 适用于 BA Scout 的原创应用源码和原创说明。第三方组件按自身许可证分发，不因本仓库的 MIT 许可而重新授权。

## Windows 便携版运行库

便携版使用 CPython 3.13.2、Tcl/Tk 8.6.15，并由 PyInstaller 6.22.3 构建。相应许可原文放在 `licenses/`，构建包保留这些文件：

- `PYTHON-LICENSE.txt`：CPython 发行版许可。
- `PYTHON-THIRD-PARTY-NOTICES.html`：Python 发行文档中对 OpenSSL、zlib、libmpdec 等组件的声明。
- `TCL-8.6.15-LICENSE.terms`、`TK-8.6.15-LICENSE.terms`：Tcl/Tk 许可。
- `PYINSTALLER-6.22.3-COPYING.txt`：PyInstaller 许可及 bootloader 例外条款。

Windows OCR / WinRT 和 Windows PowerShell 是用户操作系统提供的组件，本仓库不分发这些系统组件。

## 游戏资料和网站内容

本仓库和公开二进制发行包不包含游戏数据库提取文件、模型、纹理、图标、游戏截图或游戏日志。软件可以显示公开对局请求返回的单位名称、统计字段和来源链接，这些内容不在本项目的 MIT 授权声明之内。

Broken Arrow、BATrace 及其相关名称和商标属于各自权利人。本项目与相关开发商、发行商和网站无隶属或官方认证关系。

如以后添加外部数据或第三方代码，应先核对其许可并补充来源与必要声明。
