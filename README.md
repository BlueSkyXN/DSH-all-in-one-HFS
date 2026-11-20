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
package:    @deepseek-ai/dsh@0.2.2-rc.5
published:  2026-11-18
integrity:  sha512-cNbp12tLYLUTXaVli7zwwMjzmmEVCD1PRldg4oKtXM4+lFYckcaliMS2PU/FoThB4iAN/fKOSID0nrtRZU4Gjw==
repository: https://github.com/deepseek-ai/deepseek-harness
git tag:    dsh-v0.2.2-rc.5
```

`package-lock.json` 固定所有传递依赖；Docker 构建使用 `npm ci`，并在镜像内校验 `dsh --version`。npm 元数据未给出可回读的 `gitHead`，因此本仓库只声明 npm 产物版本、完整性以及上游同名版本 tag，不把该 tag commit 表述为 npm tarball 的字节级来源。

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
  |- settings.yaml / .credentials.yaml（由上游管理）
  `- workspace/   默认工作区

/tmp/dsh-sessions
  `- 官方 JSONL 会话日志（容器本地，重启后丢失）
```

上游 CLI 会主动拒绝 `dsh web --host 0.0.0.0`，因为 Web Surface 可创建带 Bash 和文件工具的 Agent，直接暴露会形成远程代码执行入口。包装层因此保留上游 `127.0.0.1:3080` 监听，在前面增加一个必须通过 Basic Auth 的 Nginx 入口；WebSocket、SSE 和长请求经同一入口转发。

官方 0.2.2 Web 进程还会生成进程级启动 token，并把未认证的根路径返回为 `401`。启动脚本从进程输出捕获该 token，只在 `/tmp` 生成给内层 Nginx 的跳转配置；外层 Nginx 先完成 Basic Auth，内层 Nginx 再以相对 `Location` 把浏览器带到上游 token exchange，完成后由上游签发 HttpOnly 会话 cookie。启动失败日志会先遮蔽 token。

Nginx 保留外部 `Host`，并通过 `--trusted-host` 将唯一的 `hf.space` authority 交给上游 DNS-rebinding / same-origin 防护。包装层不会重写 `Host`、`Origin` 或 `Referer` 去绕过上游的远程限制。

`tini` 只把停止信号交给监督 shell；shell 再分别通知 Nginx 和 `dsh` 并等待退出，避免上游把重复的第二个信号解释为强制终止，从而跳过会话 flush。

官方 0.2.2 版本只提供 JSONL Session persistence backend，并已移除 SQLite 会话后端。Linux 上的 JSONL 首次发布依赖 POSIX `link()` 硬链接与目录 `fsync`；HF Bucket mount 不提供这组语义。包装层因此通过 `space.cordis.yml` 把官方 JSONL backend 指到容器本地 `/tmp/dsh-sessions`，启动前显式探测硬链接；Web UI、Agent preset 和工具保持上游实现。

HF Bucket 可能把上游新建的 `.credentials.yaml` 呈现为 mode `0666`；官方 0.2.x 重启时会拒绝该权限。包装层在启动前和 readiness exchange 后都会把它限制为 `0600`，避免首次初始化后的下一次重启失败。

## Space 配置

必需：

```text
Variable: ADMIN_USERNAME
Variable: DSH_TRUSTED_HOST=blueskyxn-dsh-all-in-one-hfs.hf.space
Secret:   ADMIN_PASSWORD
```

`ADMIN_PASSWORD` 至少 16 个字符，只用于启动时在 `/tmp` 生成带随机 salt 的 Apache MD5 `htpasswd`；明文不会写入镜像、Git 或持久化 Bucket。该摘要格式是 Nginx 在 Debian 与本机验证环境都支持的兼容格式，入口 TLS 由 Hugging Face 终止。

上游启动 token 只存在于当前容器进程和 `/tmp`，不写入 Git 或持久化 Bucket。外部入口仍先通过 Nginx Basic Auth；上游 cookie 的签名密据由 `$DSH_HOME/.credentials.yaml` 持久化管理。

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
session store:  upstream JSONL backend at /tmp/dsh-sessions (container-local)
```

## 持久化约束

Space 必须将私有 Bucket 读写挂载到 `/data`。启动脚本以 UID/GID `1000` 运行，并在没有真实 mount、目录不可写或版本不匹配时失败关闭。

首次部署先创建 Bucket、挂载 `/data`，再为 `dsh/`、`dsh/home/` 和 `dsh/workspace/` 写入非敏感目录种子：

```bash
./scripts/prepare-bucket-prefix.sh --apply
```

后续只读核查使用：

```bash
./scripts/prepare-bucket-prefix.sh --check
```

不要让两个运行中的 Space 同时写同一个 `/data/dsh`。会话历史位于容器本地 `/tmp/dsh-sessions`，Space restart、rebuild 或休眠重建后会话历史不保留；Bucket 中的 `dsh/sessions.sqlite` 是旧版 SQLite 遗留文件，当前官方版本不会读取或迁移它。工作区、profile、设置和凭据文件仍走 `/data/dsh`。

## 本地验证

```bash
./scripts/static-check.sh
npm ci --omit=dev --no-audit --no-fund
npx --no-install dsh --version
```

完整 Linux/Docker 集成构建由 Hugging Face Space 执行。本地没有 Docker 时，静态检查和 npm CLI smoke 不能替代远端 build/runtime 验证。

## 平台限制与验收边界

- `cpu-basic` Space 空闲时会休眠，不适合作为无人值守的常驻 Agent。
- 会话历史是容器本地临时数据；跨重启需要保留的产物必须放在 `/data/dsh/workspace`。
- Agent 的 Bash/文件能力运行在 Space 容器和 `/data/dsh/workspace` 内；没有宿主 Docker socket。
- Protected Space 不能替代应用鉴权，实际入口保留 Basic Auth，上游 Web 进程保留官方 token/cookie 鉴权。
- 当前上游仍是 developer preview，存在破坏性兼容变更；升级必须更新 npm pin、lockfile 并重新做构建与运行验证。
- 没有 `DEEPSEEK_API_KEY` 时，只能验收构建、鉴权、Web UI 和临时会话存储，不能宣称真实模型推理完成。

## License

本仓库的包装层文件使用 GPL-3.0。DeepSeek Harness 及其 npm 包仍使用上游 MIT License；相关许可和第三方声明由上游产物保留。
