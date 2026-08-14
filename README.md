---
title: DeepSeek Harness All-in-One
emoji: 🐋
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
suggested_hardware: cpu-basic
pinned: false
license: gpl-3.0
---

# DeepSeek Harness All-in-One for Hugging Face Space

本仓库将 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)
发布的 `@deepseek-ai/dsh` Web 应用封装为 Hugging Face Docker Space。包装层不复制或修改上游业务源码，固定 npm 版本及完整依赖锁，并提供 Hugging Face 所需的单端口入口、Basic Auth 和 `/data` 持久化。

## 部署目标

- GitHub 包装仓库：<https://github.com/BlueSkyXN/DSH-all-in-one-HFS>
- Hugging Face Space：<https://huggingface.co/spaces/BlueSkyXN/DSH-all-in-one-HFS>
- 应用地址：<https://blueskyxn-dsh-all-in-one-hfs.hf.space>
- Space：Docker、Protected、`cpu-basic`
- 对外端口：`7860`
- 持久化：私有 Bucket `BlueSkyXN/dsh-all-in-one-hfs-data` 读写挂载到 `/data`

## 上游版本

包装层固定以下公开 npm 产物：

```text
package:    @deepseek-ai/dsh@0.1.0-rc.6
published:  2026-08-13
integrity:  sha512-brpZfED7ieRa2PQ5tUxMhHrM1pb2CmKFVM/f6yMULBDMicahk+Z2OsHgTwTDnoiZm23Ftu9rQz0NN4pflaoJcg==
repository: https://github.com/deepseek-ai/deepseek-harness
```

`package-lock.json` 固定所有传递依赖；Docker 构建使用 `npm ci`，并在镜像内校验 `dsh --version`。上游当前没有对应 Git tag 或 GitHub Release，npm 元数据也未给出可回读的 `gitHead`，因此本仓库只声明已验证的 npm 产物版本和完整性，不把某个 Git commit 表述为该产物的精确来源提交。

## 运行结构

```text
Hugging Face proxy
        |
        v
Nginx Basic Auth :7860
        |
        v
dsh web 127.0.0.1:3080

/data/dsh
  |- home/        持久化用户主目录
  |- profiles/    dsh 自动初始化的 profile 与插件状态
  |- sessions/    会话及投影数据
  |- settings.yaml / .credentials.yaml（由上游管理）
  `- workspace/   默认工作区
```

上游 CLI 会主动拒绝 `dsh web --host 0.0.0.0`，因为 Web Surface 可创建带 Bash 和文件工具的 Agent，直接暴露会形成远程代码执行入口。包装层因此保留上游 `127.0.0.1:3080` 监听，在前面增加一个必须通过 Basic Auth 的 Nginx 入口；WebSocket、SSE 和长请求经同一入口转发。

Nginx 保留外部 `Host`，并通过 `--trusted-host` 将唯一的 `hf.space` authority 交给上游 DNS-rebinding / same-origin 防护。包装层不会重写 `Host`、`Origin` 或 `Referer` 去绕过上游的远程限制。

## Space 配置

必需：

```text
Variable: ADMIN_USERNAME
Variable: DSH_TRUSTED_HOST=blueskyxn-dsh-all-in-one-hfs.hf.space
Secret:   ADMIN_PASSWORD
```

`ADMIN_PASSWORD` 至少 16 个字符，只用于启动时在 `/tmp` 生成带随机 salt 的 Apache MD5 `htpasswd`；明文不会写入镜像、Git 或持久化 Bucket。该摘要格式是 Nginx 在 Debian 与本机验证环境都支持的兼容格式，入口 TLS 由 Hugging Face 终止。

本机部署控制值保存在被 Git 忽略且权限为 `0600` 的 `local/hfs-targets/production.env`。不要把该文件复制为仓库根 `.env`：上游会自动读取 workspace 的 `.env`，并按设计拒绝其中的 `DSH_*` 和网络启动变量。根目录 `.env.example` 只是键名模板。

要执行真实模型请求，还需要：

```text
Secret:   DEEPSEEK_API_KEY
Variable: DEEPSEEK_BASE_URL（仅在使用非默认兼容端点时设置）
```

DeepSeek Harness 会按上游顺序从继承环境、`$DSH_HOME/.credentials.yaml`、workspace `.env` 和 `$DSH_HOME/.env` 读取凭据。远程浏览器对 `settings.*`、`credentials.*`、`llm.discoverModels` 等接口仍会收到上游规定的 `403`；Basic Auth 不会提升这些 loopback-only 能力。Space Secret 是当前远程部署的主要模型凭据入口。

## HFS 分类

```text
standard:       HFS v3.0
project_class:  preview
target_role:    primary
sovereignty:    port
lane:           artifact
version_source: tag (npm package version)
visibility:     protected Space / private bucket
```

## 持久化约束

Space 必须将私有 Bucket 读写挂载到 `/data`。启动脚本以 UID/GID `1000` 运行，并在没有真实 mount、目录不可写或版本不匹配时失败关闭。

首次部署先创建 Bucket、挂载 `/data`，再写入非敏感目录种子：

```bash
./scripts/prepare-bucket-prefix.sh --apply
```

后续只读核查使用：

```bash
./scripts/prepare-bucket-prefix.sh --check
```

不要让两个运行中的 Space 同时写同一个 `/data/dsh`。会话持久化需要用“写入 -> Space restart -> 回读”证明，Bucket 已挂载或 Space 为 `RUNNING` 本身不等于数据已经通过重启验证。

## 本地验证

```bash
./scripts/static-check.sh
npm ci --omit=dev --no-audit --no-fund
npx --no-install dsh --version
```

完整 Linux/Docker 集成构建由 Hugging Face Space 执行。本地没有 Docker 时，静态检查和 npm CLI smoke 不能替代远端 build/runtime 验证。

## 平台限制与验收边界

- `cpu-basic` Space 空闲时会休眠，不适合作为无人值守的常驻 Agent。
- Agent 的 Bash/文件能力运行在 Space 容器和 `/data/dsh/workspace` 内；没有宿主 Docker socket。
- Protected Space 不能替代应用鉴权，实际入口始终保留 Basic Auth。
- 当前上游仍是 developer preview，存在破坏性兼容变更；升级必须更新 npm pin、lockfile 并重新做构建与运行验证。
- 没有 `DEEPSEEK_API_KEY` 时，只能验收构建、鉴权、Web UI 和持久化层，不能宣称真实模型推理完成。

## License

本仓库的包装层文件使用 GPL-3.0。DeepSeek Harness 及其 npm 包仍使用上游 MIT License；相关许可和第三方声明由上游产物保留。
