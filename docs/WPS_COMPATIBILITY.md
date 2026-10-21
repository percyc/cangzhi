# WPS 文字兼容

上传入口和 WebDAV 支持 `.wps`（不区分大小写），限额、权限与原件下载沿用现有规则。
现有 WebDAV 来源若已显式配置扩展名，需在来源配置中添加 `.wps`；不会静默修改旧来源。
外部 `/api/upload` 使用相同文件校验与后台处理，不需要额外模型配置。

处理路径：原件保存 → 兼容容器检查 → LibreOffice 临时转 DOCX → 结构解析 →
基础摘要/分类/标签、切片与索引。PDF 版式预览单独生成；仅无原生正文且存在正文图片时
使用现有 Word PDF/OCR 回退，仍受 OCR 配置、页数和质量门槛约束，不强制全篇付费 OCR。
解析版和切片展示转换后结构，原件始终可下载核对。失败保留原件及已有结果。

## 范围与局限

- 支持 OLE Word / OOXML 兼容容器，经当前 LibreOffice Writer 能成功转换的 `.wps`。
- `.wps` 后缀也被 Microsoft Works 使用，不能据此推断内容一定属于金山 WPS。
  未知历史容器明确拒绝，不按纯文本兜底。请在 WPS 中另存为 DOCX 或 PDF 后上传。
- 不承诺旧版私有格式、密码保护、损坏文件或复杂排版完整还原；嵌入对象、修订、
  宏与高级版式的处理局限沿用 DOCX 解析器，不将宏作为知识执行。
  实测合成样本转为 Word97 时，末尾表格可能在导出阶段被展平；转换成功不等于
  结构无损，重要表格请核对原件/PDF，优先上传 WPS 直接保存的标准 DOCX。
- `.et` 和 `.dps` 不属于本次范围；WPS 保存的标准 DOCX/XLSX 继续走原有流程。
- Worker 需要现有的 LibreOffice Writer；不新增依赖、数据库迁移或模型渠道。
  部署由维护者执行，需更新 API、Worker 和 Web；仅更新 migrate 镜像不会带来本功能。
- 本批自动验证使用合成兼容文件，不等于已验证所有金山原生历史格式；真实文件应逐份核对。

设计依据与公共上传增量契约见 [ADR-031](ADR-031-wps-writer.md)。

## 开发验证

在已安装依赖的隔离 Worker 测试运行时设置
`CANGZHI_TEST_WORD_OCR_INTEGRATION=1`，运行
`python -m pytest apps/api/tests/test_wps.py apps/worker/tests/test_word_ocr.py`，
包含真实 LibreOffice 的 Word97 兼容容器往返、PDF 预览及本地图片 OCR。
前端在 `apps/web` 执行 `npm run test:ux`、`npm run lint` 和 `npm run build`；
`CANGZHI_TEST_WPS_VIEW=1 node tests/browser/document-views.cjs` 使用合成接口与独立
浏览器验证四视图保留、预览修订失效及窄屏页签，不访问部署实例或真实模型。
