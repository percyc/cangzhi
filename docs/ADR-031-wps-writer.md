# ADR-031：WPS 文字文件兼容

状态：按维护者新增 WPS 兼容要求实施（2026-10-21）。

上传契约增量接受 `.wps`，浏览器、外部上传和 WebDAV 共用现有校验；权限、
大小限额、原件下载和 Scope Key 行为不变。仅支持 WPS 文字，暂不包含 `.et`、`.dps`。

原件及哈希不变。兼容 OLE Word 和 OOXML 容器的 `.wps` 经 Worker 已有的
LibreOffice 转换为临时 DOCX，再复用结构解析、基础理解、切片与检索。
PDF 预览保留源扩展名并复用隔离配置、超时与存储边界；图片型正文复用 Word OCR。
不增加模型调用策略或独立收费通道，不在读取时隐式转换。

依据：[LibreOffice 官方过滤器表](https://help.libreoffice.org/latest/en-US/text/shared/guide/convertfilters.html)
将 `doc wps` 列为 Word 97–2003 的扩展名；Microsoft Works 的同名扩展不是
金山 WPS 的全面支持证据。未知容器不回退为文本，明确建议另存 DOCX/PDF；
旧版私有、加密、损坏和复杂排版不承诺无损兼容。转换失败保留原件和已有派生结果。

验证采用合成兼容文件、失败边界和实际 LibreOffice 转换；没有真实金山历史样本时
不得宣称所有原生 WPS 版本都已验证。无需数据库迁移或新增依赖。
