# 思脉（Simai）代码审核与修改报告

审核日期：2026-08-15  
原始文件：`simai.tar.gz`  
原始文件 SHA-256：`ac037f9c9bff43ed0f66cb845be56094a3114062dbefccaeb012928c47bda791`

## 1. 结论

原始版本已经实现了思脉的大部分业务骨架，但**不能直接部署到腾讯云**。审核确认了两项严重问题：Web 解锁认证可绕过；OpenClaw Plugin 使用了与目标版本不一致的接口，真实宿主中无法可靠工作。此外还存在 Channel 身份未贯穿校验、显式/驾驶记录可能丢失、数据库并发、SQLCipher 临时文件、导出权限、树与关系版本一致性等问题。

本次已对代码做实质性修复，并增加回归测试。修订版的结论是：

- **可以进入本地 WSL 功能试验。** 核心数据库、候选确认、树、关系、查询、导出、备份及 Plugin 的离线契约测试已通过。
- **暂不建议直接作为腾讯云生产版本。** 上线前必须在你的真实 OpenClaw `2026.7.x`、腾讯微信 Channel 和现有 Qwen ASR 配置上完成第 7 节的实机验收。
- “数据库和备份被复制后不可读”的静态加密目标已按 SQLCipher、加密密钥头及加密备份实现。它不防御已经控制运行中主机、服务进程或解锁后内存的管理员/恶意程序。

## 2. 与产品设计的符合度

| 能力 | 审核结果 | 说明 |
|---|---|---|
| Web / CLI 主动记录 | 已实现 | 模型轻度整理或原文候选，确认后才写正式树 |
| 微信文字主动记录 | 已实现，待实机验证 | `simai_capture` Tool 返回确认卡；Core 不可用时公钥加密暂存 |
| 微信语音记录 | 条件完成 | 复用现有 ASR，只读取 `message:preprocessed.bodyForAgent`；必须先确认 ASR 确实位于媒体预处理链 |
| 驾驶模式 | 已实现，待实机验证 | 进入驾驶模式后，后续匹配消息均按 `explicit` 加密进入待确认箱，不经过保守的聊天筛选 |
| 普通聊天每日整理 | 已实现 | 实时加密捕获指定来源的用户消息；每日处理截止点前所有未处理项，不依赖“会话结束” |
| 精确 Channel 绑定 | 已实现 | Plugin 与 Python Core 均校验 channel、account、sender、conversation、group；字段缺失时拒绝 |
| 用户确认 | 已实现 | 支持确认、拒绝、暂缓及修改标题、正文、类型、父节点/目标节点 |
| 持续生长的树 | 已实现 | 单父节点、无限层级、循环检测、版本历史、创建/补充/修订/移动/合并/归档 |
| 节点间语义关系 | 已实现 | 有向/无向关系、来源/置信度/理由、绑定节点版本；节点变更后旧关系变为 `stale` |
| 查询与查看 | 已实现 | Web/CLI/Tool，FTS5、加密库内 Embedding、自然语言问答及节点引用 |
| 选择性导出 | 已实现 | simai-json、Markdown、OPML、GraphML、JSON Canvas；支持子树、类型、时间和关系过滤 |
| 数据库与备份加密 | 已实现 | SQLCipher、错误密钥测试、HMAC 认证备份清单、权限收紧 |
| 后台模型配置 | 已实现 | YAML 可为各任务选择专用 Agent，并通过 `x-openclaw-model` 覆盖 OpenClaw default |
| Web 修改模型/定时配置 | 未实现 | 当前 Web 只读显示与健康检查；配置仍通过 YAML/OpenClaw Cron 管理 |
| 长期主动洞察 | 未完成 | 当前具备结构化数据、搜索和问答基础；跨月主动建议、盲区分析仍属后续能力 |

## 3. 原始版本主要问题与修复结果

### 3.1 严重与高风险问题

| 问题 | 风险 | 修改结果 |
|---|---|---|
| 已解锁时 `/api/unlock` 不再校验口令 | 任意非空口令可取得完整 Web 会话 | 无论数据库是否已解锁都重新验证口令；新增错误口令回归和失败限速 |
| 原包源码与目录普遍为 `0777` | 同机其他用户可篡改加密、Web 或 Plugin 代码 | 最终包规范为目录 `0755`、源码 `0644`；运行数据目录 `0700`、敏感文件 `0600`；移除依赖与缓存 |
| Plugin 使用不存在/不匹配的 OpenClaw API | 真实宿主中无法注册、捕获或定时 | 改为 typed `message_received`、internal `message:preprocessed`、Tool factory/`execute`；定时改为 command cron |
| Plugin 与 Core 只靠同名 `binding_id` | 两边配置不一致时可能采错账号/用户 | schema v2 密文包含完整身份元组；Core 入站及每日解密时再次精确核验 |
| 显式与普通聊天共用同一种待处理消息 | “记录一下”或驾驶思想可能被每日保守模型丢弃 | 增加 `capture_mode`；显式/驾驶消息必生成原文候选，普通聊天才进入每日筛选 |
| 驾驶状态未影响后续普通消息 | 驾驶中的连续想法仍可能被过滤 | 驾驶期间后续每条匹配消息均按 `explicit` 加密暂存；扩展自然语言开关指令 |
| 单个 SQLCipher 连接被多个线程无统一边界使用 | 并发提交、关闭或读取状态不确定 | 统一读锁/事务锁；每日任务增加进程互斥；服务保持单 Worker 部署 |
| SQLCipher 未禁止文件型临时存储 | 查询临时文件可能是明文 | 每个连接强制并验证 `temp_store=MEMORY`、`cipher_status=1`、`secure_delete=ON` |
| 明文导出默认权限及清理范围错误 | 同机泄露；加密导出被误删 | 随机唯一文件名、`O_EXCL`、`0600`、目录 `0700`；TTL 仅清理由思脉生成的明文导出 |
| 备份清单无密钥认证 | 攻击者可改写清单掩盖文件删除 | 使用从数据库密钥域分离出的 HMAC 认证清单；核验精确文件集合、哈希、符号链接及错误密钥 |
| 搜索使用 GET 查询参数 | 搜索词可能写入访问日志 | Web 与 Plugin 搜索改为 POST JSON；服务默认关闭 access log |
| CLI 将思想正文放入 argv | shell history 和进程列表泄露 | 主动记录/查询默认从 stdin 或交互输入读取 |

### 3.2 数据一致性与功能问题

- 修复候选页未采用模型建议动作/父节点，导致默认错误创建根节点的问题。
- 修复候选执行 append/revise 时忽略用户修改标题和节点类型的问题。
- 拒绝非法节点类型，并增加数据库触发器作为第二道约束。
- 移动、修订、恢复节点后，将依赖旧 revision 的关系标记为 `stale`。
- Embedding 检索只使用节点当前 revision；更新失败时不会继续把旧向量当作当前内容。
- 归档整棵子树；创建、移动、更新、合并、恢复和关系写入均拒绝非 active 目标，避免“活跃子节点挂在隐藏父节点”状态。
- 修复多条件导出过滤顺序、同秒文件覆盖、加密导出 TTL 误删和恢复文件权限。
- 每日任务加入有界批次、Prompt 上限、损坏项隔离、队列条数/总字节限制和处理幂等。
- 显式记录与被动捕获发生重复时优先保留显式意图，提交成功后再删除 sealed ciphertext。
- 个人词典不再把用户词条拼入 inline `onclick`，修复存储型 XSS 路径。
- 初始化后的恢复包在用户确认保存后清除页面变量、隐藏 DOM 和 Blob URL；敏感响应增加 `no-store`。

### 3.3 OpenClaw 集成修改

- 第一阶段只在 `message_received` 建立经过 channel/account/sender 校验的身份信封。
- 第二阶段从 `message:preprocessed` 取得 ASR/媒体处理后的最终正文，并按 messageId 与完整身份双向关联。
- 不把 sessionKey 当身份凭据；只有两边都提供时才要求一致。
- Core、Unix Socket 暂不可用或超时时，Plugin 使用 Vault 公钥生成 libsodium sealed box；不建立明文 spool。
- Core 明确拒绝身份时不会使用 fallback 绕过。
- Plugin Token 与 Vault Header 必须是当前服务用户所有的普通文件，不得是符号链接或开放 group/world 权限。
- Core URL、Gateway URL 强制 loopback，HTTP 客户端不继承系统代理。
- 每日任务由 OpenClaw command cron 调用无正文输出的 Worker；模型调用由思脉自己的任务路由控制。

## 4. 加密与数据边界

修订版采用以下结构：

1. 用户口令经 Argon2id 派生 KEK，只用于解封随机 Vault Root Key。
2. Vault Root Key 经 HKDF 分离出 SQLCipher、来源摘要、审计、候选片段等用途密钥。
3. SQLCipher 主库同时保存树、版本、关系、FTS5 和 Embedding，不建立明文外部索引。
4. 锁定期间只使用 sealed-box 公钥写短期密文箱；Plugin 不持有解密私钥。
5. 备份数据库仍为 SQLCipher 密文，并带密钥认证 manifest；恢复时同时验证正确密钥和错误密钥行为。

以下数据不由思脉加密承诺覆盖：OpenClaw 自己的聊天历史、Provider/Gateway 运行日志、已经选择下载的明文导出、运行中进程内存。正式环境仍需关闭含正文的 OpenClaw/反向代理日志，并保护主机与进程权限。

## 5. 测试与验证

最终回归环境：Python 3.12、Node.js 24、SQLCipher 4.12 community。模型调用使用测试替身，不向外部模型发送审核数据。

| 检查 | 结果 |
|---|---|
| `pytest -q` | 3/3 通过；1 条第三方 TestClient 弃用警告 |
| `python tests/smoke_test.py` | 75/75 通过 |
| `python tests/api_test.py` | 46/46 通过；当前审计沙箱不允许真实 AF_UNIX，相关单项明确跳过 |
| `ruff check simai tests` | 通过 |
| `ruff format --check simai tests` | 通过 |
| Python `compileall` | 通过 |
| Python wheel 构建 | 通过（最终回归前已完成一次无依赖构建） |
| TypeScript 编译 | 通过 |
| Node Plugin tests | 15/15 通过 |
| Web inline JavaScript 语法检查 | 通过 |

新增回归覆盖认证绕过、SQLCipher 能力、错误密钥、密钥文件权限、树循环/非活跃节点、revision 与关系失效、候选编辑、Embedding 版本、导出权限/唯一名/TTL/组合过滤、备份篡改/恢复权限、每日幂等/显式优先/积压边界、Plugin 身份关联、Core 超时、加密 fallback、队列配额、驾驶模式及真实 Tool 注册形态。

## 6. 已知限制

以下不是本次包中已经验证完成的能力：

1. **ASR 挂载方式未知。** 如果 Qwen 脚本配置在 `tools.media.audio.models`，当前 Hook 可直接取得转写；如果它是聊天模型运行后调用的普通 Tool，必须增加该 Tool 的 `after_tool_call` 适配后才能捕获语音正文。
2. **未在真实 OpenClaw SDK 上编译。** Plugin 的 TypeScript 类型是对 `2026.7.1` 官方接口的结构化镜像；仍需运行宿主的 plugin validate/inspect 和真实消息测试。
3. **Web 配置目前只读。** 模型 Agent、后端模型覆盖、每日时间和来源绑定由 YAML/OpenClaw Cron 修改，修改后应重启并重新做健康检查。
4. **单进程假设。** 当前互斥锁为进程内锁；生产必须保持一个 Simai Web Worker。若未来横向扩展，需要数据库 lease/任务队列。
5. **模型调用会占用写事务。** 单用户低并发可保证一致性，但模型慢或不可用时会阻塞其他写操作；后续可改为 outbox + 异步 enrichment/retry。
6. **Embedding/自动关系暂无持久重试队列。** 用户的正式节点不会丢失，但模型故障期间可能暂时缺少语义索引或自动关系。
7. **审计不是外部不可抵赖日志。** 目前有应用层 append-only 触发器和逐行 HMAC，但没有覆盖整表删除的链式锚点/外部见证。
8. **依赖尚未做跨平台哈希锁定。** Node 使用 package-lock；Python 仍是版本范围。腾讯云镜像应锁定并记录实际 SQLCipher/Python wheel 哈希后再部署。
9. **数据库迁移框架仍较轻。** 干净初始化和当前 schema 已测试；已有历史数据库升级必须先备份并做独立迁移演练。
10. **高级“外挂大脑”能力尚未产品化。** 当前已有可供 RAG/大模型使用的树、关系、版本、导出与问答，但主动跨月提醒、思维盲区和策略提炼需要后续任务与评估集。

## 7. 腾讯云上线门槛

必须全部通过后再启用微信被动采集：

1. 在腾讯云安装的确切 OpenClaw 版本中运行 Plugin 验证/加载命令，确认无 manifest、Hook 或 Tool 契约错误。
2. 做一次**只记录元数据、不记录正文**的微信 payload probe，锁定 `channelId= openclaw-weixin`、accountId、Yu 的 sender/from、conversationId、messageId 和 sessionKey 实际字段。
3. 设置 `session.dmScope=per-account-channel-peer`，并证明其他微信账号、其他发送者、群聊和缺字段消息都不会进入 sealed inbox。
4. 分别发送微信文字和语音，确认 `message:preprocessed.bodyForAgent` 是 Qwen3.5-Omni-Plus 的最终转写而不是 `[Audio]` 占位内容。
5. 验证方便时主动记录会返回确认卡；验证驾驶模式连续发送三条内容，三条均进入 explicit 待确认箱。
6. 锁定 Vault 后发送消息，确认磁盘只有 `0600` sealed ciphertext；复制文件后无法读取；Web 解锁后能补处理。
7. 手工执行 command cron，确认只处理 cutoff 前所有未处理项，无候选时 `NO_REPLY`，提醒精确投递到指定 account/to。
8. 使用另一微信 Channel、另一 accountId、另一 sender、群聊和错误 conversationId 做负向测试，全部必须拒绝。
9. 做一次备份、离线复制、错误口令失败、正确口令恢复及对象数核对。
10. 检查 OpenClaw、systemd、Uvicorn 和反向代理日志，确保无思想正文、搜索词、口令、Token、Prompt 或模型输出。

## 8. 部署建议

- 先使用 `local_wsl` Profile，通过 Web/CLI 完成创建候选、确认、移动、关系、搜索、问答、导出、备份和恢复演练。
- 腾讯云使用独立 Linux 用户运行 Simai 与 Plugin；代码/配置不可 group/world writable，`~/.simai` 为 `0700`。
- Web 只监听 loopback，通过 SSH Tunnel 访问；不要把管理端口直接暴露公网。
- 为思脉创建无消息发送能力、工具最小化的专用 OpenClaw Agent。Gateway HTTP API 是 operator 级边界，应保持 loopback，并妥善保护 token。
- 初始化返回的恢复包只保存到离线介质。遗失口令且没有恢复包时，设计上无法恢复数据。

## 9. 审核依据

- [OpenClaw v2026.7.1 Plugin Hooks](https://github.com/openclaw/openclaw/blob/v2026.7.1/docs/plugins/hooks.md)
- [OpenClaw v2026.7.1 Message Hook Types](https://github.com/openclaw/openclaw/blob/v2026.7.1/src/plugins/hook-message.types.ts)
- [OpenClaw v2026.7.1 Message Hook Mapper](https://github.com/openclaw/openclaw/blob/v2026.7.1/src/hooks/message-hook-mappers.ts)
- [OpenClaw v2026.7.1 Internal Hooks](https://github.com/openclaw/openclaw/blob/v2026.7.1/docs/automation/hooks.md)
- [OpenClaw v2026.7.1 Audio Flow](https://github.com/openclaw/openclaw/blob/v2026.7.1/docs/nodes/audio.md)
- [OpenClaw v2026.7.1 Cron Jobs](https://github.com/openclaw/openclaw/blob/v2026.7.1/docs/automation/cron-jobs.md)
- [OpenClaw OpenAI-compatible HTTP API](https://docs.openclaw.ai/gateway/openai-http-api)
- [Tencent openclaw-weixin](https://github.com/Tencent/openclaw-weixin)
- [SQLCipher Design](https://www.zetetic.net/sqlcipher/design/)
- [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)
