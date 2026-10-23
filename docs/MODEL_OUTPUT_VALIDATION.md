# 模型输出解析与安全错误提示

OpenAI兼容接口和Ollama的查询规划、基础整理及最终回答共用消息提取和JSON对象解析。
支持字符串、text/output_text文本分段、可选代码围栏及单个对象前后的说明文字；
不读取reasoning_content/thinking，也不把reasoning类型分段当作答案。

必须返回唯一、完整的JSON对象。数组、重复键、多个对象、损坏/截断JSON不尝试
抽取内部对象或拼造字段；上游明确报告length/max_tokens时，即使残留JSON可解析也拒绝。
解析成功不等于内容合格：原有字段类型/长度、分类白名单、答案引用白名单及证据要求
继续校验。证据不足标记的既有保守归一化不变，不把不足回答升级为充分回答。

网页、REST和MCP复用问答服务的静态提示。仍使用provider_failed和既有HTTP/流式
错误形式，不增加公开字段；内部AIProviderError保持illegal_output大类并增加白名单reason：

| reason | 提示方向 |
| --- | --- |
| response_shape | 接口响应结构不兼容 |
| empty_response | 没有可用正文，推理过程不作为答案 |
| truncated_response | 上游截断，缩小范围或检查模型输出限制 |
| refused_response | 模型拒绝生成 |
| invalid_json | 非有效且唯一的JSON对象 |
| invalid_fields | 字段缺失、类型不符或超过长度限制 |
| missing_citations | 缺少有效引用，拒绝展示 |
| unknown_citations | 引用越出本轮证据范围，拒绝展示 |

最终回答失败日志只记录category和reason，不记录问题、答案、字段值、密钥、模型地址、
供应商响应或推理正文。不自动增加模型重试，不因格式失败更换模型或提高输出上限。

验证使用合成响应与真实服务层回归。历史日志只有illegal_output，不能据此倒推之前
某次失败的确切原因；上线后若模型仍违反字段或引用要求，会显示具体拒绝原因。
本修复不是对任意模型输出正确性的保证，也未使用真实用户资料进行额外模型调用。
