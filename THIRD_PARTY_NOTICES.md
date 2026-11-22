# 第三方组件与数据范围

仓库的 `LICENSE` 适用于 BA Scout 的原创应用源码和原创说明。第三方组件按自身许可证分发，不因本仓库的 MIT 许可而重新授权。

## Windows 便携版运行库

便携版使用 CPython 3.13.2、Tcl/Tk 8.6.15，并由 PyInstaller 6.22.3 构建。相应许可原文放在 `licenses/`，构建包保留这些文件：

- `PYTHON-LICENSE.txt`：CPython 发行版许可。
- `PYTHON-THIRD-PARTY-NOTICES.html`：Python 发行文档中对 OpenSSL、zlib、libmpdec 等组件的声明。
- `TCL-8.6.15-LICENSE.terms`、`TK-8.6.15-LICENSE.terms`：Tcl/Tk 许可。
- `PYINSTALLER-6.22.3-COPYING.txt`：PyInstaller 许可及 bootloader 例外条款。

Windows OCR / WinRT 和 Windows PowerShell 是用户操作系统提供的组件，本仓库不分发这些系统组件。

## PP-OCRv4 姓名识别模型

准备阶段的姓名识别使用 PaddleOCR 的中文移动端 PP-OCRv4 识别模型。RapidOCR 将模型转换为 ONNX 格式，BA Scout 原样分发 `rapidocr-onnxruntime 1.4.4` 中的 `ch_PP-OCRv4_rec_infer.onnx`。文件大小为 10,857,958 字节，SHA-256 为 `48fc40f24f6d2a207a2b1091d3437eb3cc3eb6b676dc3ef9c37384005483683b`。这与 RapidOCR 登记的 `ch_PP-OCRv4_rec_mobile` 一致。[上游发行包](https://pypi.org/project/rapidocr-onnxruntime/1.4.4/) · [模型登记及哈希](https://github.com/RapidAI/RapidOCR/blob/v3.10.0/python/rapidocr/default_models.yaml#L317-L319)

原始权重的版权归百度及适用的 PaddleOCR 权利人。模型按 Apache License 2.0 分发。RapidOCR 转换、封装及其所加元数据的版权归 RapidOCR Authors。模型中用于解码的 6,623 个字符与 PaddleOCR 的 `ppocr_keys_v1.txt` 内容和顺序一致。随包提供 PaddleOCR 上游许可证原文。[模型许可和归属](https://rapidai.github.io/RapidOCRDocs/latest/en/model_licenses/) · [上游字符表](https://github.com/PaddlePaddle/PaddleOCR/blob/release/2.7/ppocr/utils/ppocr_keys_v1.txt) · [随包许可证](licenses/PP-OCRv4-APACHE-2.0.txt)

Copyright (c) 2016 PaddlePaddle Authors. All Rights Reserved.
Copyright (c) 2021 RapidOCR Authors. All rights reserved.
Copyright (c) 2021 RapidAI. All rights reserved.

BA Scout 调用 Windows 提供的 `Windows.AI.MachineLearning` 在用户电脑上运行模型。系统运行库由 Windows 提供。上述许可声明针对分发的模型及其内嵌字符表；发行包不含训练数据集。[Microsoft Windows ML API 文档](https://learn.microsoft.com/uwp/api/windows.ai.machinelearning)

## 游戏资料和网站内容

本仓库和公开二进制发行包不包含游戏数据库提取文件、游戏模型、纹理、图标、游戏截图或游戏日志。软件可以显示公开对局请求返回的单位名称、统计字段和来源链接，这些内容不在本项目的 MIT 授权声明之内。

Broken Arrow、BATrace 及其相关名称和商标属于各自权利人。本项目与相关开发商、发行商和网站无隶属或官方认证关系。

如以后添加外部数据或第三方代码，应先核对其许可并补充来源与必要声明。
