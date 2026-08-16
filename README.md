# 思脉（Simai）

一棵持续生长、可追溯、可查询的个人思想树。产品基线见
`simai_product_design_v1.md`；本次代码审计及修复记录见
`REVIEW_AND_CHANGES.md`。

## 组成

| 目录 | 内容 |
|---|---|
| `simai/` | Python 3.12 核心服务：加密存储、候选确认、树/版本/关系、搜索问答、导出备份、Web API 与 CLI |
| `simai/web/static/` | 单用户 Web 管理端；口令只在这里解锁，不经微信 |
| `plugin/` | OpenClaw 原生 Plugin：精确来源绑定、双阶段捕获、加密收件箱及 `simai_*` Tools |
| `config/` | WSL / 腾讯云配置示例 |

## 本地 WSL 快速开始

~~~bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

mkdir -p ~/.simai
cp config/simai.yaml ~/.simai/simai.yaml
# 先编辑 profile、绝对路径、模型 Agent 和精确 source_bindings；
# 带占位符的 binding 保持 disabled，代码会拒绝启用占位身份。

simai doctor
simai init
simai serve
~~~

浏览器打开 `http://127.0.0.1:18880`。服务启动时始终锁定，手工输入口令后才可
查询、确认和导出。初始化返回的一次性恢复包必须转移到离线安全位置。

CLI 不把思想或问题放入参数，避免 shell history 和进程列表泄露：

~~~bash
printf '%s\n' '记录一下：产品要聚焦单用户体验' | simai capture
printf '%s\n' '我对单用户产品的观点是什么？' | simai query
simai tree
simai export --format markdown --encrypt
~~~

## 模型配置

思脉只调用本机 OpenClaw Gateway，不保存模型 Provider API Key。先在 OpenClaw
中创建无消息发送能力、工具最小化的专用 Agent，再在 YAML 配置：

~~~yaml
models:
  source: openclaw
  inherit_main_default: false
  task_agents:
    capture: simai
    daily_extract: simai
    graph_routing: simai
    query: simai
    query_relevance: simai
  # 可选择与 OpenClaw default 不同的后台模型；值通过
  # x-openclaw-model 交给 OpenClaw 路由，Provider 凭据仍由 OpenClaw 管理。
  task_models:
    capture: qwen/qwen3.5-plus
    daily_extract: qwen/qwen3.5-plus
  embedding_model: embeddinggemma-300m
  # Gateway 开启鉴权时填写其 token 文件；必须为 0600 且不可是符号链接。
  gateway_token_file: ~/.openclaw/gateway.token
~~~

OpenClaw 的 OpenAI-compatible chat/embedding HTTP endpoints 可能默认关闭；按已安装
版本的官方文档启用后，在 Web“配置与状态”中检查四个任务。思脉强制 Gateway 为
loopback，且不继承系统 HTTP/SOCKS 代理。健康检查会显示请求的后端覆盖值；标准
OpenAI-compatible 响应中的 `model` 是 Agent 目标，并不是后端 Provider 的独立证明。
需要审计实际 Provider 时，应同时核对 OpenClaw 自身的运行日志/遥测。

## OpenClaw 与微信

Plugin 的构建、配置、精确身份字段和 22:30 command-cron 示例见
[`plugin/README.md`](plugin/README.md)。生产多账号先设置：

~~~bash
openclaw config set session.dmScope per-account-channel-peer
~~~

现有语音链保持不变：

~~~text
微信语音 → OpenClaw 媒体预处理 → 自定义脚本
        → Qwen3.5-Omni-Plus 转写 → 当前聊天模型
~~~

Plugin 只读取 `message:preprocessed` 的最终用户文本，不保存音频或 OpenClaw 聊天
记录。如果当前转写脚本是聊天模型运行后才调用的普通 Tool，而不是媒体预处理链，
必须先做 Phase 0 payload probe 并增加对应 Tool 结果适配；否则不要上线被动采集。

## 安全边界

- 主库强制 SQLCipher；FTS5 与 Embedding 也在加密库内。
- `temp_store=MEMORY`、`cipher_status=1`、外键、FTS5 和完整性检查不满足即拒绝运行。
- 口令经 Argon2id 解封 Vault Root Key；派生密钥仅驻留服务进程内存。
- 锁定/服务不可用期间，Plugin 使用公开密钥写 schema-v2 sealed box，不创建明文队列。
- Plugin 与核心每天处理时都会核验 `channel + account + sender + conversation + group`。
- 正式树写入必须由用户确认；被动聊天只生成候选。
- Web 仅绑定 loopback；腾讯云通过 SSH Tunnel 访问，不直接暴露管理端口。
- 明文导出为 0600 并按 TTL 清理；口令加密导出不会被 TTL 自动删除。

## 测试

~~~bash
pytest -q
python tests/smoke_test.py
python tests/api_test.py

cd plugin
npm ci
npm test
~~~

测试不需要真实模型。当前审计环境禁止 Unix Domain Socket，因此 API 套件会明确
跳过该单项；Python sealed-box 流程和 Node 加密 fallback 仍分别执行。正式部署前还
必须在实际 WSL/OpenClaw/微信环境完成 `REVIEW_AND_CHANGES.md` 中的上线门槛。
