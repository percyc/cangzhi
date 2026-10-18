# 运行版本与发布节点

## 页面里看什么

右上角账户菜单显示网页构建版本，点击进入设置页的“版本信息”；各设置页导航下方
也有同一入口。展开后显示Web/API的完整提交号、构建时间（UTC）与是否含未提交修改。
提交号及版本标签均一致才显示一致；API不可达、旧版缺少信息或脏构建均不能算验收通过。
这里核对的是运行镜像，不是服务器Git目录；打开详情才请求API，不调用模型。

Worker也携带镜像标签和构建信息，启动日志记录同一信息。页面暂不收集Worker心跳，
因此Web/API一致不代表Worker已升级。运维可分别核验：

```sh
docker compose exec -T worker python -m apps.api.core.build_info
docker compose exec -T api python -m apps.api.core.build_info
```

## 如何构建与离线启动

推荐`make build` / `make upgrade`，它们通过`with-build-info.sh`在构建机读取Git提交，
一次性将版本、提交、UTC时间与脏状态作为构建参数传给四种镜像（含migrate）。
精确标签构建显示标签；标签之后显示`VERSION+短提交`；工作区不干净追加`.dirty`。
`VERSION`是下一发布基线，不等于声称已经发布该标签。源码目录需要有Git元数据。

只想构建指定服务：

```sh
bash scripts/with-build-info.sh docker compose build api worker migrate web
```

普通`docker compose build`若未传构建参数，显示“未标记构建/unknown”，不猜测提交号。
没有Git的源码包构建者可以显式传`CANGZHI_VERSION`、`CANGZHI_REVISION`、
`CANGZHI_BUILD_TIME`、`CANGZHI_BUILD_DIRTY`四个构建参数。不要在运行时覆盖这些字段。
所有版本信息随镜像导出，内网无需Git或互联网；加载后仅启动，不再运行构建脚本：

```sh
docker load -i cangzhi-images.tar
docker compose up -d --no-build --pull never
```

## 发布约定

维护者授权：重要交付节点完成验证后自动创建带说明的标签，并同步到Gitea/Gitee/GitHub。
不为每次提交打标签，不移动旧标签。兼容修复与体验完善用补丁号；有明确成组能力交付
再提升次版本；不因开发提交数量自动升级主版本。发布前确认远端标签未占用。

推荐顺序：完成功能、测试和文档→提交并确保干净→以拟发布版本构建镜像→部署核验→
在该源码提交创建标签→合并main并推送main及该标签。之后部署记录提交可以继续前进，
标签仍指向镜像构建的源码提交，不把文档后补提交误标为运行代码。

当前仅保证版本标识和Web/API提交核对，不是依赖锁、供应链签名或全部服务健康证明。
