# ADR-039：Office 容器的上传 MIME 兼容

状态：按维护者 2026-10-28 的 DOCX 上传兼容修复要求实施。

2026-10-28 现场补充：已取得上传文件部分的 `application/wps-office.docx`
声明。仅在扩展名为 `.docx` 时将这个精确别名规范化为标准 DOCX MIME；不接受
任意 `application/wps-office.*`，其他扩展名使用该声明仍拒绝。沿用既有大小写、
空格和参数规范化。原件字节、后台解析及所有权限/限额不变。

现有上传入口按扩展名与客户端声明的 MIME 校验，不使用 libmagic。
DOCX 声明为 application/zip 时会返回 415；application/octet-stream 和缺省
类型原本已支持。这个条件可独立于 CPU 架构复现，不能仅凭 ARM 环境推断实际根因。

浏览器、REST 文件上传与 WebDAV 共用原校验函数：统一去除 MIME 参数、首尾空格并
忽略大小写；仅对 .docx、.xlsx、.wps 接受 application/zip、application/x-zip、
application/x-zip-compressed，并转为对应的 Office MIME 保存。其他明确的类型冲突
仍返回 415，未知扩展名和 .zip 文件不开放。通用二进制与缺省类型保持原兼容行为。

类型头仅作上传兼容判断，不作为文件内容有效性的证明。沿用先保存原件、后台格式
专用解析的生命周期：普通 ZIP 改名为 DOCX、损坏或加密文件由解析器拒绝，不以纯文本
兜底；已保存原件可供下载和纠正。上传成功不代表解析成功。此补丁不增加 API 内同步
解压、转换或模型请求，不改变鉴权、Scope Key、幂等、文件限额及响应结构。

测试覆盖真实合成 DOCX 的多种声明、上传/下载字节一致性、格式解析、外部写入和
WebDAV，并保留错扩展、类型冲突、空文件及超限拒绝测试。ARM 的转换组件、内存、
磁盘、代理与实际问题文件仍需要目标主机日志或复测；amd64 测试不宣称 ARM 验收。
