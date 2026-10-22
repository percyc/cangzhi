# 藏知品牌图标

## 设计与适用范围

图标以环抱的轮廓表现收藏与保管，以展开的书页表现知识与阅读；轮廓同时呼应
Cangzhi 的首字母 C。深墨蓝延续现有界面的稳重感，蓝色书页保持与交互强调色一致。
图像中不生成汉字，旁边的“藏知 / Cangzhi”仍使用真实文字，保证小尺寸可读和无障碍。

本图使用 **内置 imagegen** 生成，未使用 CLI/API 密钥模式，也没有以手写 SVG
替代生成结果。原始 PNG 保留透明通道；应用资源只做离线尺寸与 PNG 格式导出，
没有重新描画或添加元素。此记录不代表商标相似性检索或注册审查。

## 文件与使用

| 文件 | 用途 |
|---|---|
| `cangzhi-icon-v1-source.png` | 1254 × 1254 生成原图，不作为页面大图加载 |
| `../../apps/web/public/brand/cangzhi-icon-v1-96.png` | 导航 36px、登录/初始化 44px，兼顾高分屏 |
| `../../apps/web/public/brand/cangzhi-icon-v1-32.png`、`-48.png` | 浏览器标签页 favicon |
| `../../apps/web/public/brand/cangzhi-icon-v1-180.png` | Apple 触屏图标 |
| `../../apps/web/public/brand/cangzhi-icon-v1-512.png` | README 和品牌展示 |

网页共用 `BrandMark`，图标 URL 通过 `withBasePath` 处理根路径和网关子路径。
小图预生成，不通过图片优化接口临时转换，不增加运行时模型请求。旁边已有名称时
图标使用空 alt，首页链接仍有完整无障碍名称。此批不改变外部 DSH 插件，也不增加
PWA 安装/离线功能。历史界面截图保留其对应版本，不伪改为新品牌。

在已安装项目依赖的环境重新导出（无需联网或模型调用）：

```bash
cd apps/web
npm run assets:brand
```

后续换图应保留原图并递增资产文件名版本，更新 `lib/brand.ts` 和对应测试，
避免浏览器继续使用旧 favicon。这里只交付资源和代码，实际部署状态以项目状态页为准。

验证命令：在 `apps/web` 执行 `npm run test:ux`、`npm run lint`、
`npm run typecheck`、`npm run build`，再执行 `npm run test:browser:brand`。
浏览器验收使用隔离本地服务和合成身份，不登录实际实例、不调用模型；需已安装
Chromium 或 Google Chrome。与其他 Next 开发/构建测试串行运行，避免共享 `.next` 冲突。

## 最终生成提示词

```text
Use case: logo-brand
Asset type: production app icon and favicon for 藏知 (Cangzhi), a personal, controllable knowledge hub.
Primary request: Create ONE original, beautifully restrained brand symbol that expresses knowledge carefully collected and ready to open. A simple folded/open book motif held by a broad C-shaped enclosing stroke, integrated as one coherent geometric mark, not a collage of symbols.
Style/medium: crisp flat vector-like graphic rendered as a high quality square raster, strong silhouette, generous negative space, contemporary editorial identity.
Composition: one centered icon on a deep ink navy rounded-square tile. Tile nearly fills the square canvas with about 4% transparent margin on each side. The symbol is optically centered and fills roughly 65% of the tile. Use a few broad substantial shapes with open gaps that remain recognizable at 16 and 32 pixels.
Color palette: deep ink navy matching #020617 for the tile, soft white for the principal book/enclosure; one restrained blue #3B82F6 page-fold accent, only if it stays visually simple.
Backdrop: truly transparent outside the rounded-square tile.
Constraints: a single finished icon only, front-facing, flat fills, clean edges. No typography, no Chinese characters, no letters written as text, no wordmark. No mockup, variants, presentation board, decorative border, gradients, texture, shadow, 3D, tiny lines, AI brain, robot, circuit nodes, stars or watermark. The result must work on both white and dark interfaces.
```
