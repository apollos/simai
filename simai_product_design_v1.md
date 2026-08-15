# 思脉（Simai）产品设计文档

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 日期 | 2026-08-14 |
| 状态 | MVP 实施基线 |
| 部署顺序 | 本地 WSL 验证 → 腾讯云正式部署 |
| 核心入口 | OpenClaw Web、CLI、指定微信 OpenClaw Channel |

> 思脉不是一个保存零散笔记的工具，而是一棵持续生长、可追溯、可查询的个人思想树。

---

## 1. 产品定义

思脉用于长期记录 Yu 在工作、生活、技术、组织管理等方面形成的个人思想。输入可以是主动表达的灵感，也可以来自日常与 OpenClaw 的聊天。

系统负责：

1. 捕捉用户表达。
2. 对口语进行轻度整理和纠错。
3. 提取真正属于用户的观点、判断、问题和启发。
4. 提议新建、补充、修正或合并到已有主题。
5. 经用户确认后写入持续生长的思维树。
6. 自动建立节点间的语义联系。
7. 提供树形浏览、关系查看、搜索、问答和选择性导出。

### 1.1 产品目标

- 走路、开车或临时想到内容时，可以低负担记录。
- 避免思想以聊天、便签和单条笔记形式分散。
- 保留观点从产生到修正、深化和取代的演化过程。
- 让大模型能够基于用户真实、确认过的思想提供分析。
- 数据库、索引和备份文件被复制后仍无法读取。

### 1.2 非目标

- 不建设第二套微信 Channel。
- 不建设第二套语音转写服务。
- 不保存微信语音文件。
- 不替代 OpenClaw 的聊天记录和会话管理。
- 不把助手回答直接保存为用户观点。
- 不以整篇文章、PDF 或网页原文作为主要知识来源。
- 不允许模型未经确认自动合并、移动或删除正式节点。
- 首版不建设多人协作、团队权限或公开分享。

---

## 2. 市场产品启示与差异

| 产品类型 | 可借鉴能力 | 思脉的不同点 |
|---|---|---|
| XMind 等思维导图 | 树形展开、收起、局部聚焦、视觉布局 | 思脉的树跨越多年持续生长，不以一次性制图为中心 |
| Obsidian 等双链笔记 | 本地数据、开放格式、链接和关系图 | 思脉自动归类、保留节点版本，并区分主树与语义关系 |
| Heptabase 等视觉知识库 | 卡片、白板、关联知识的空间化查看 | 思脉以个人思想演化为真源，不以素材和研究资料汇总为主 |
| Plaud 等 AI 录音笔记 | 低负担采集、转写和总结 | 思脉复用现有微信语音链，且不保存录音和完整会话 |

XMind 已支持将文字、文件和对话整理为思维导图；Obsidian 强调本地开放文件、链接和图视图；Heptabase 采用卡片、白板和双向链接组织知识；Plaud 重点解决语音采集、转写和总结。思脉吸收这些能力，但核心对象是“经过用户确认、持续演化的个人思想”。参考：[XMind AI](https://xmind.com/ai)、[Obsidian](https://obsidian.md/)、[Heptabase](https://heptabase.com/)、[Plaud Intelligence](https://www.plaud.ai/pages/plaud-intelligence)。

---

## 3. 核心设计原则

### 3.1 用户思想与 AI 内容分离

- 只有用户说过、写过或明确确认的内容，才能成为正式思想节点。
- 助手回答只用于理解上下文，不能自动成为用户观点。
- AI 自动发现的节点关系必须标记为 AI 生成。
- AI 本次推导出的结论不能冒充用户历史观点。

### 3.2 正式写入必须确认

用户确认的是：

- 整理后的内容。
- 节点类型。
- 建议挂载位置。
- 新建、补充、修正或合并动作。

普通语义关系可以自动记录为“AI 生成”；会改变思想含义或树结构的动作仍需确认。

### 3.3 树与图同时存在

每个正式节点只有一个主父节点，保证整体仍然是一棵可以理解和导出的大树。同时允许任意节点之间存在带类型和属性的语义联系。

### 3.4 原始材料最小化

- 不保存音频。
- 不复制完整聊天记录。
- 只临时保存等待处理的用户文字密文。
- 候选确认或拒绝后，默认删除原始片段。
- 正式思想、版本、关系和必要来源指纹长期保存。

### 3.5 安全失败

- 来源身份字段缺失时不采集。
- 模型输出不符合结构化约束时不写库。
- 数据库锁定时不尝试绕过口令。
- 定时任务失败时不推进处理水位线。

---

## 4. 用户场景

### 4.1 主动语音记录

用户在微信中说“记录一下……”并发送语音。现有 OpenClaw 完成转写，思脉基于转写文字生成确认卡。

### 4.2 驾驶或不方便确认

系统照常生成候选。用户不回复时，候选进入加密待确认箱，在每日提醒中集中处理。

### 4.3 普通聊天自动提取

用户平时向 OpenClaw 提问、讨论工作或表达判断。系统每天处理一次指定 Channel 的增量消息，从中提取可能值得长期保存的内容。

### 4.4 查阅与追溯

用户通过 Web 或绑定的微信身份查询：

- 某个主题下有哪些观点。
- 某个观点过去如何变化。
- 哪些思想互相支持、冲突或存在依赖。
- 最近一段时间关注点发生了什么变化。

### 4.5 选择性导出

用户通过 Web 选择整棵树、某个子树、时间范围或节点集合，导出后交给其他大模型继续分析。

---

## 5. 总体架构

~~~text
OpenClaw Web / CLI / 指定微信 Channel
                    │
                    ▼
              OpenClaw Plugin
        ├── 消息来源精确校验
        ├── 获取最终用户文字
        ├── 注册思脉 Tool
        └── 定时任务与定向提醒
                    │
                    ▼
              思脉核心服务
        ├── 加密临时收件箱
        ├── 内容整理与候选提取
        ├── 树形归类与版本管理
        ├── 语义关系生成
        ├── 搜索与问答
        └── 导出
                    │
                    ▼
       SQLCipher 数据库 + 加密备份
                    │
                    ▼
        Web 管理端 / CLI / 查询 Tool
~~~

### 5.1 组件选择

| 组件 | 建议实现 |
|---|---|
| OpenClaw 集成 | TypeScript Plugin |
| 核心服务 | Python 3.12 Web Service |
| Web API | FastAPI 类轻量框架 |
| CLI | 与核心服务共用业务层 |
| 主数据库 | SQLCipher 加密 SQLite |
| 临时密文箱 | 成熟密码库提供的 sealed box |
| 全文检索 | SQLite FTS5，位于 SQLCipher 内 |
| 语义检索 | Embedding 保存在 SQLCipher 内，解锁后内存计算 |
| 前端 | 单用户 Web 管理界面 |
| 定时 | OpenClaw Cron 调用思脉 Worker |
| 生产进程 | 腾讯云使用 systemd 管理 |

首版不引入 Redis、消息队列、Elasticsearch、独立向量数据库或 Neo4j。

---

## 6. 输入与处理工作流

### 6.1 主动语音记录

现有链路保持不变：

~~~text
微信语音
→ OpenClaw 调用现有自定义脚本
→ Qwen3.5-Omni-Plus 转写
→ 转写文字交给当前聊天模型
→ 调用 simai_capture
→ 整理并生成确认卡
→ 用户确认
→ 写入思维树
~~~

当前 WSL 环境中的既有脚本为：

~~~text
/home/yu/.openclaw/bin/qwen_omni_transcribe.py
~~~

正式接入前需要确认该脚本在 OpenClaw 中的实际挂载方式：

- 如果脚本配置在 tools.media.audio.models 中，转写会在聊天模型运行前完成，思脉从 message:preprocessed 读取最终 bodyForAgent。
- 如果脚本由聊天模型作为普通 Tool 调用，转写发生在模型运行后，思脉应监听该 Tool 的 after_tool_call 并关联当前消息。

两种情况都继续使用同一个脚本和 Qwen3.5-Omni-Plus，不新增 ASR，也不要求重新建设微信语音链路。

思脉不读取或保存音频文件，也不改变 OpenClaw 对音频文件的默认生命周期。

### 6.2 主动文字记录

~~~text
OpenClaw Web / CLI / 微信文字
→ simai_capture
→ 轻度整理
→ 推荐主树位置和动作
→ 用户确认
→ 正式写入
~~~

本地 WSL 阶段主要使用该流程验证核心功能。

### 6.3 普通聊天每日提取

不判断会话是否结束，改为每天处理一次：

~~~text
白名单来源的最终用户文字
→ 公钥加密后进入 sealed inbox
→ 每日 22:30 读取所有截止时间前的未处理项
→ 提取候选思想
→ 写入 SQLCipher 待确认箱
→ 删除已处理的临时密文
→ 向精确来源发送一次提醒
→ 用户确认后写入正式思维树
~~~

sealed inbox 是为了在数据库锁定或服务重启期间仍不丢消息而设置的短期处理队列，不提供聊天浏览、检索或长期留存能力。完整会话仍只由 OpenClaw 管理；思脉在成功生成候选并提交处理回执后删除对应密文。

默认行为：

- 普通聊天观察默认开启。
- 时区为 Asia/Shanghai。
- 每天 22:30 运行。
- 默认保留最近 30 分钟消息到下一批。
- 处理范围是“所有未处理消息”，不是只查询当天或昨天。
- 失败时不推进水位线，下次继续处理。
- 同一消息重复输入不能产生重复候选。
- 已通过主动记录处理的消息标记为 handled_explicitly，每日提取自动跳过。
- 没有候选时默认不提醒。
- 有候选时每天只提醒一次。
- 微信最多显示 5 条摘要，更多内容在 Web 中处理。

如候选必须依赖助手回答才能理解，可在处理时通过 OpenClaw 的会话接口临时读取相邻上下文；上下文只在内存使用，不写入思脉。OpenClaw 提供受权限控制的会话历史读取能力。[OpenClaw Session Tools](https://docs.openclaw.ai/concepts/session-tool)

### 6.4 自适应确认

系统不需要推断用户是否驾驶：

- 用户立即回复：当场确认、编辑或拒绝。
- 用户没有回复：候选保持待确认状态。
- 每日任务：将所有待确认内容合并提醒。

候选不会因为超时自动写入正式树。

同时提供显式驾驶模式。用户可以说“我在开车，接下来只记录”进入驾驶模式；系统只回复简短的“已进入待确认箱”，不发送完整确认卡。用户说“结束驾驶模式”后恢复即时确认，并展示本次积累的候选。驾驶模式只改变交互，不降低来源校验和加密要求。

---

## 7. OpenClaw 集成设计

### 7.1 为什么采用 Plugin 加 Tool

纯 MCP 或普通 Tool 只能在模型主动调用时工作，无法可靠完成普通聊天的旁路采集。因此首版采用：

- Plugin：来源校验、消息观察、最终文字获取、定时任务和定向提醒。
- Tool：主动记录、确认、搜索和查询。
- 核心服务：模型处理、存储、树、关系和 Web。
- MCP：作为未来供其他客户端访问的可选接口，不是首版前提。

### 7.2 两阶段消息捕获

为兼容微信语音预处理和多 Channel，当前 OpenClaw 2026.7.1-2 环境采用两阶段关联：

1. typed message_received 只校验 Channel、accountId 和发送者，建立身份信封。
2. internal message:preprocessed 取得媒体处理完成后的最终 bodyForAgent。
3. 如果 ASR 是模型调用的普通 Tool，则改从该 Tool 的 after_tool_call 获取转写结果。
4. 身份信封与最终文字匹配成功后，才将文字公钥加密并写入临时密文箱。

messageId 是首选关联键。缺少 messageId 时，使用精确 sessionKey、时间窗口和同一 Session 的串行顺序。两阶段事件可能乱序到达，实现必须支持双向 upsert。runId 不作为稳定的首选关联键。

before_agent_run 只作为兼容性后备；如果使用，需要按当前 OpenClaw 版本开启相应的会话访问权限，而且不能绕过 message_received 阶段完成的 accountId 身份认证。任一关键身份无法证明时拒绝采集。

OpenClaw Plugin Hooks 支持观察入站消息、模型运行前 prompt、模型选择和消息投递。[OpenClaw Plugin Hooks](https://docs.openclaw.ai/plugins/hooks)

当前版本的实现校验依据还包括：[OpenClaw v2026.7.1 Internal Hooks](https://github.com/openclaw/openclaw/blob/v2026.7.1/docs/automation/hooks.md)、[OpenClaw v2026.7.1 Audio Flow](https://github.com/openclaw/openclaw/blob/v2026.7.1/docs/nodes/audio.md)。

### 7.3 精确来源绑定

生产白名单至少包含：

~~~text
channel/provider = openclaw-weixin
accountId        = 指定微信机器人账号
senderKey        = Yu 对应的微信 from_user_id
conversationId   = 指定私聊会话（如插件提供）
~~~

OpenClaw 原始字段统一规范化为：

~~~text
sender_key  = ctx.senderId ?? event.from
binding_key = HMAC(channel | account_id | sender_key | conversation_id)
~~~

如果当前插件提供稳定的 conversationId，生产环境必须将其纳入 binding_key；只有插件确实不提供时才允许省略。数据库和配置使用 snake_case，OpenClaw Hook 中保留原始 camelCase 字段名。

规则：

- 必须同时匹配 Channel、账号和发送者。
- 腾讯微信插件的发送者规范化规则为 senderKey = ctx.senderId ?? event.from。
- sessionKey 只作消息关联，不作身份认证。
- 群聊默认拒绝。
- 未知 Channel 默认拒绝。
- 提醒必须显式指定同一个 Channel、accountId 和接收者。
- 禁止使用 last、最近会话或通配符作为生产提醒目标。

腾讯云首次联调时发送一条测试消息，只显示和记录非内容元数据，确认当前 OpenClaw 与微信插件版本实际提供的字段，然后锁定配置。

如果 OpenClaw 中有多个微信账号，启用：

~~~bash
openclaw config set session.dmScope per-account-channel-peer
~~~

腾讯微信插件明确建议使用账号、Channel 和发送者隔离私聊上下文。[Tencent openclaw-weixin](https://github.com/Tencent/openclaw-weixin)、[OpenClaw Channel Routing](https://docs.openclaw.ai/channels/channel-routing)

### 7.4 定时任务

由 OpenClaw command cron 在 22:30 调用思脉 Worker，Worker 自己执行增量、幂等和事务逻辑。command cron 不把思想正文作为 Cron prompt 或输出。

如果 Worker 通过独立 simai Agent 调用模型，该调用仍可能形成 OpenClaw 自己的独立 Session 或运行历史，但不会写入日常聊天 Session。该历史继续由 OpenClaw 按默认行为维护，属于本文第 15.5 节声明的安全边界。

任务要求：

- 显式设置 Asia/Shanghai。
- Worker 显式使用 simai Agent 或模型配置。
- 如果改用 agent cron，必须限制为思脉所需工具，并禁止无关消息发送工具。
- 发送提醒时显式指定 Channel、accountId 和接收者。
- 任务输出不得包含思想正文或口令。

OpenClaw 定时任务支持 Cron 时区、独立模型、fallback 和精确投递目标。[OpenClaw Automations](https://docs.openclaw.ai/automation/cron-jobs)

---

## 8. 内容理解与提炼

### 8.1 可提取内容

- 个人观点或判断。
- 对组织、产品、市场和技术的思考。
- 决策及决策依据。
- 假设、待验证问题和风险判断。
- 工作方法、原则或复盘结论。
- 灵感、方案和改进建议。
- 长期关注的问题。
- 阅读、会议或讨论后形成的个人启发。

### 8.2 默认排除内容

- 问候和社交寒暄。
- 单纯让助手执行操作的命令。
- 临时查天气、价格、时间等一次性查询。
- 助手的回答或推断。
- 没有个人加工的长篇粘贴材料。
- 密码、验证码、密钥和个人身份凭证。
- 无法脱离上下文理解的短回复。
- 内容重复且没有新增信息的表达。

### 8.3 轻度整理规则

允许：

- 删除语气词、重复句和无意义停顿。
- 修正错别字、标点和明显语法错误。
- 将口语改成简洁、完整的书面表达。
- 拆分多个不同思想。
- 根据上下文补全明确指代。

禁止：

- 增加用户没有表达的新事实。
- 改变数字、时间、对象和专有名词。
- 删除否定、条件、范围或不确定性。
- 将疑问改成结论。
- 将模型建议改写成用户观点。

专有名词和常见语音识别错误进入个人词典，例如产品名、模型名、客户名和组织术语。

### 8.4 候选结构

~~~json
{
  "candidate_id": "C-20260814-001",
  "candidate_type": "idea",
  "source_excerpt": "加密临时保存的最小用户片段",
  "normalized_content": "整理后的思想",
  "title": "建议节点标题",
  "proposed_action": "create_child",
  "proposed_parent_ids": ["N-102", "N-008"],
  "confidence": 0.86,
  "needs_clarification": false
}
~~~

模型必须输出符合固定 Schema 的结构化结果。无法解析、字段越界或节点不存在时，不执行写入。

---

## 9. 用户确认设计

确认卡展示：

1. 原始用户片段。
2. 整理后的内容。
3. 思想类型。
4. 建议动作。
5. 推荐挂载位置及完整路径。
6. 可能关联的已有节点。

用户操作：

- 确认。
- 修改文字后确认。
- 更换父节点。
- 改为新一级主题。
- 选择补充或修正已有节点。
- 暂缓。
- 拒绝。

微信适合完成简单确认；批量调整、移动节点、查看历史和导出只在 Web 完成。

---

## 10. 思维树归类机制

### 10.1 主树约束

- 每个正式节点只有一个 parent_id。
- 根级主题的 parent_id 为空。
- 层级不设上限。
- 父节点不能是自身或自身后代。
- 默认软删除或归档，不直接物理删除。

### 10.2 归类动作

| 动作 | 含义 | 是否必须确认 |
|---|---|---|
| create_root | 新建一级主题 | 是 |
| create_child | 挂到已有主题下 | 是 |
| append | 为已有节点增加补充版本 | 是 |
| revise | 修正已有观点 | 是 |
| merge | 合并两个节点 | 是 |
| move | 移动节点或分支 | 是 |
| cross_link | 只新增语义关系 | 普通关系可自动记录 |

### 10.3 归类步骤

1. 对新内容生成 Embedding。
2. 检索全树语义相近节点。
3. 补充检索候选节点的父节点、兄弟节点和祖先节点。
4. 返回最多 3 个候选位置。
5. 模型判断新建、补充、修正或可能合并。
6. 用户确认主位置和动作。
7. 在同一事务中写入节点、修订和审计事件。

当没有足够相关的已有主题时，提议新建一级主题。相似度不足时不得为了减少分支而强行合并。

### 10.4 节点版本

修改观点不覆盖历史。每次正式变更生成新版本，记录：

- 修改前后内容。
- 当时的父节点。
- 节点类型。
- 变更动作。
- 来源候选。
- 确认时间。
- 内容哈希。

树界面默认显示当前版本，同时可以查看时间线和恢复历史版本。

---

## 11. 节点语义关系

### 11.1 结构关系与语义关系分离

~~~text
主树关系：一个节点在树中的唯一位置
语义关系：任意两个节点之间的含义联系
~~~

语义关系可以连接：

- 父子节点。
- 同一父节点下的两个子节点。
- 不同分支的节点。
- 新观点与历史观点。
- 工作、技术、生活等不同一级主题。

父子关系本身不重复写入 relations；导出图格式时可临时转换为 contains 边。

### 11.2 首批关系类型

| 类型 | 含义 | 方向 |
|---|---|---|
| related_to | 一般相关 | 无向 |
| supports | 支持或提供依据 | 有向 |
| contradicts | 存在冲突 | 可有向 |
| refines | 进一步细化 | 有向 |
| qualifies | 增加条件或适用范围 | 有向 |
| depends_on | 依赖 | 有向 |
| applies_to | 适用于 | 有向 |
| inspired_by | 受到启发 | 有向 |
| supersedes | 新观点取代旧观点 | 有向 |

### 11.3 关系属性

每条关系保存：

- 起点节点和起点版本。
- 终点节点和终点版本。
- 关系类型及方向。
- 简短、可检查的理由。
- 模型置信度。
- 生成模型和时间。
- 来源为 AI 或用户。
- 状态。

不保存模型内部思维链，只保存简短理由。

### 11.4 自动生成规则

新节点或节点新版本确认后：

1. 检索父节点、兄弟节点和祖先节点。
2. 对全树执行语义 Top-K 检索。
3. 模型只返回真正有意义的关系。
4. 每个新版本默认最多自动记录 3 条。
5. 低于置信度阈值的关系不保存。

自动关系状态为 ai_generated，在界面中以虚线显示。用户可以确认、修改或否决。

supersedes、分支移动、节点合并以及任何会改变旧观点有效状态的动作，不能静默生效。

### 11.5 关系版本有效性

关系必须绑定两端节点版本。例如：

~~~text
A revision 3 supports B revision 2
~~~

任一端内容发生变化时，旧关系进入 stale 状态并重新评估，不能继续无条件视为有效。

---

## 12. 数据存储设计

### 12.1 主存储选择

主数据库采用 SQLCipher 加密 SQLite：

~~~text
SQLCipher SQLite
├── 邻接表主树
├── 独立关系表
├── 追加式节点版本
├── 全文索引
├── Embedding
└── 审计事件
~~~

不使用所谓“专门思维导图数据库”。OPML、GraphML 和 JSON Canvas 适合交换或展示，不能同时承担加密、事务、修订、来源、查询和确认流程。

SQLite 支持递归 CTE 遍历树和图；SQLCipher 对数据库页面进行透明加密。[SQLite Recursive CTE](https://www.sqlite.org/lang_with.html)、[SQLCipher Design](https://www.zetetic.net/sqlcipher/design/)

首版不使用 Neo4j，原因是：

- 单用户规模不需要独立图数据库服务。
- 主树唯一父节点仍需业务层约束。
- 修订、确认和来源仍需自行实现。
- WSL、腾讯云、备份和加密运维更复杂。

### 12.2 核心表

#### nodes

~~~text
id
parent_id
node_type
title
current_revision_id
sort_order
state
created_at
updated_at
~~~

#### node_revisions

~~~text
id
node_id
revision_no
parent_id
node_type
title
body
change_type
source_candidate_id
content_hash
created_at
~~~

#### relations

~~~text
id
from_node_id
to_node_id
relation_type
is_directed
label
rationale
from_revision_id
to_revision_id
confidence
origin
model_profile
state
supersedes_relation_id
valid_from
valid_to
~~~

#### candidates

~~~text
id
source_binding_id
candidate_type
source_excerpt_ciphertext
normalized_content
proposed_action
proposed_parent_ids
status
batch_date
created_at
decided_at
~~~

#### source_bindings

~~~text
id
binding_key
channel
account_id
sender_key
conversation_id
enabled
passive_capture
created_at
~~~

#### source_cursors

~~~text
source_binding_id
last_successful_time
last_message_hmac
last_job_status
updated_at
~~~

#### source_receipts

~~~text
source_binding_id
message_hmac
capture_mode
handled_explicitly
captured_at
processed_at
batch_id
~~~

source_binding_id 与 message_hmac 组成唯一键，由主动记录和每日观察两条链路共同使用，不保存消息正文。主动记录成功后设置 handled_explicitly，后续每日任务自动跳过同一消息。

#### embeddings

~~~text
node_id
revision_id
model_id
dimensions
vector_blob
updated_at
~~~

#### audit_events

~~~text
id
event_type
entity_type
entity_id
before_json
after_json
candidate_id
confirmed_at
created_at
~~~

#### view_layout

~~~text
view_id
node_id
x
y
width
height
collapsed
updated_at
~~~

view_layout 只保存显示位置，不参与思想语义。

### 12.3 状态机

候选状态：

~~~text
pending → confirmed
        → rejected
        → snoozed → pending
~~~

候选进入 confirmed 或 rejected 后，必须清除 source_excerpt_ciphertext，只保留最终内容、决定结果和消息 HMAC。

关系状态：

~~~text
ai_generated → confirmed
             → rejected
             → stale
~~~

节点状态：

~~~text
active → archived
       → merged
~~~

### 12.4 数据库约束与事务

必须启用并验证以下约束：

- SQLite foreign_keys 开启。
- node_revisions 的 node_id 与 revision_no 唯一。
- nodes.current_revision_id 必须指向该节点自己的修订。
- 移动节点前使用递归查询确认目标父节点不是自身或自身后代。
- active 关系必须具有唯一业务键；无向关系先规范化两端节点顺序。
- 任何硬删除都不能级联删除仍被版本或关系引用的数据。
- nodes 当前快照、node_revisions、relations 和 audit_events 在同一事务提交。
- 每日候选写入、source_receipts 更新和 source_cursors 推进在同一事务提交。
- 只有上述事务成功后才删除对应 sealed inbox 密文；失败则保留重试。

本地初始化必须验证 SQLCipher cipher_version、cipher_status、外键和 FTS5 能力，任一项缺失时拒绝创建正式数据库。

---

## 13. 搜索、查询与上下文生成

### 13.1 搜索

- 标题和正文全文检索。
- 语义相似度检索。
- 按一级主题、节点类型、时间和状态过滤。
- 查询某节点的祖先、后代、兄弟和关联节点。
- 查询某时间段内新增或变化的思想。

FTS 和 Embedding 均保存在 SQLCipher 数据库中，不建立明文外部索引。

### 13.2 自然语言问答

基本流程：

~~~text
用户问题
→ 关键词和语义联合检索
→ 获取相关节点当前版本、路径、必要历史和关系
→ 后台模型生成回答
→ 返回节点引用
~~~

回答必须区分：

1. 用户已经确认的思想。
2. AI 自动生成但未确认的关系。
3. 模型在本次回答中形成的新推论。

回答引用格式至少包含节点 ID、完整路径和版本号。

### 13.3 长期分析

后续可在用户主动发起时提供：

- 关注点变化。
- 某个观点的演化时间线。
- 反复出现的问题。
- 不同主题间的潜在联系。
- 观点内部的冲突和未验证假设。
- 思考较少或长期未更新的领域。

这些分析结果默认不直接写入思想树；用户确认后才可成为新节点。

---

## 14. Web 管理端

服务启动后首先进入锁定页。解锁成功后显示以下页面。

### 14.1 待确认箱

- 查看当天及历史候选。
- 对比原始片段与整理结果。
- 批量确认、拒绝或暂缓。
- 修改内容和挂载位置。
- 查看相似节点。

### 14.2 思维树

- 展开和折叠任意分支。
- 以任意节点为局部根节点。
- 新增、编辑、移动、归档节点。
- 查看节点路径、类型和更新时间。
- 显示未确认候选数量。

### 14.3 节点详情

- 当前内容。
- 历史版本。
- 来源类型和时间。
- AI 自动关系。
- 用户确认关系。
- 与其他节点的路径。

### 14.4 关系视图

- 显示局部关系图，不默认一次展示全图。
- 按关系类型、方向和状态过滤。
- 区分实线用户确认关系和虚线 AI 关系。
- 修改、确认、否决和隐藏关系。

### 14.5 搜索与问答

- 关键词搜索。
- 语义搜索。
- 条件过滤。
- 自然语言查询。
- 显示回答所引用节点。

### 14.6 配置与运行状态

- 查看来源白名单。
- 开关普通聊天观察。
- 修改每日处理时间。
- 选择后台 Agent 和模型。
- 测试模型健康状态。
- 查看最近定时任务结果。
- 备份、恢复和导出。
- 手动锁定数据库。

---

## 15. Web 解锁与加密

### 15.1 解锁流程

~~~text
启动思脉 Web Service
→ 服务处于 Locked 状态
→ 用户通过管理网页输入口令
→ 解封 SQLCipher 数据密钥和 sealed inbox 私钥
→ 密钥只保存在服务进程内存
→ 开放树、查询、整理和导出能力
~~~

规则：

- 口令不通过微信输入。
- 口令不发送给 OpenClaw 或大模型。
- 口令不写入配置、环境变量、命令行参数或日志。
- 服务重启后重新锁定。
- 用户可以在 Web 中手动锁定。
- Web 登录会话过期不自动锁定数据库，避免破坏每日任务。
- 数据库保持解锁，直到服务停止或用户手动锁定。

这是为“每天 22:30 自动处理”选择的运行模式：服务完成一次 Web 解锁后，数据密钥可以长期驻留在该服务进程内存中；Web 查询本身仍要求重新登录。它防御数据库、备份和停机文件被复制，不防御已控制运行中进程或主机管理员权限的攻击者。

可以配置空闲自动锁定，但启用后，数据库在 22:30 仍处于锁定状态时只发送提醒并等待下次 Web 解锁，不会自动整理。

### 15.2 密钥设计

- 初始化时生成随机 Vault Root Key，数据库密钥不直接等于用户口令。
- 用户口令通过 Argon2id、随机 salt 和版本化参数生成 Key Encryption Key。
- 使用 XChaCha20-Poly1305 封装 Vault Root Key。
- 使用 HKDF 从 Vault Root Key 派生 SQLCipher Key、Inbox Private-Key Wrap Key 和 Audit HMAC Key，避免不同用途共用同一密钥。
- sealed inbox 使用成熟密码库的公钥密封方案。
- OpenClaw Plugin 只持有加密公钥，不能解密历史消息。
- 解密私钥由用户口令封装，解锁后才进入内存。

密钥头文件可以公开保存，但必须包含：

~~~text
format_version
kdf_type
kdf_parameters
salt
wrap_algorithm
wrap_nonce
wrapped_vault_root_key
sealed_inbox_public_key
wrapped_sealed_inbox_private_key
~~~

修改口令时只重新封装 Vault Root Key，不重新加密整个数据库。初始化时应允许用户生成一次性离线恢复包；恢复包只交给用户下载，不保存在腾讯云。用户放弃恢复包且遗忘口令时，系统无法恢复数据。

MVP 不使用腾讯云 KMS 自动解锁。

### 15.3 锁定期间

- Plugin 仍可将白名单用户文字加密写入 sealed inbox。
- 密文箱不包含助手回复、音频或附件。
- sealed box 只提供保密性，不证明发送者身份；只有先通过 Channel、accountId 和 senderKey 白名单验证的 Plugin 才能写入。
- 每条密文内部包含 schema_version、binding_id、messageId、sessionKey、采集时间和用户正文。
- 对 binding_id 与 messageId 的 HMAC 建立唯一去重记录，缺少 messageId 时使用受控的后备指纹。
- Plugin 通过 Unix Socket 和文件权限访问本地写入口，不开放网络写入端口。
- 每条密文先写临时文件并同步落盘，再原子改名，避免服务崩溃留下半条记录。
- 22:30 任务发现数据库锁定时不处理、不推进水位线。
- 微信只收到不含正文的“思脉处于锁定状态”提示。
- Web 解锁后自动补处理所有积压密文。

### 15.4 Web 访问

本地 WSL：

- 默认只监听 127.0.0.1。
- Windows 浏览器通过本机端口访问。
- 不开放局域网访问。

腾讯云：

- 管理端仍只监听 127.0.0.1。
- 使用 SSH Tunnel 或私有 VPN 访问。
- 不将解锁页面直接暴露到公网。
- 如后续必须公网访问，需要 HTTPS、独立登录认证、访问白名单和速率限制。

### 15.5 安全边界

思脉保证：

- SQLCipher 数据库副本不可直接读取。
- 备份副本不可直接读取。
- sealed inbox 副本只有密文。
- 外部全文和向量索引不产生明文副本。

思脉不保证：

- OpenClaw 自己维护的聊天历史已被思脉加密。
- Qwen 或其他模型服务商不保留请求日志。
- 用户主动下载的明文导出继续受保护。
- 已完全控制运行中服务器及进程内存的攻击者无法读取内容。

---

## 16. 后台模型配置

### 16.1 配置原则

- 复用 OpenClaw 已配置的 Provider、模型目录和凭证。
- 思脉不重复保存 API Key。
- 默认不继承 main Agent 当前 default 模型。
- 首版使用独立 simai Agent。
- OpenClaw 兼容接口调用使用 openclaw/simai，而不是直接传任意原始模型名。
- simai Agent 的 primary 和 fallback 可独立于 main Agent 设置。

需要更细分时，可增加：

~~~text
simai-capture
simai-daily
simai-query
~~~

每个 Agent 使用不同模型，但仍由 OpenClaw 管理凭证。

### 16.2 模型任务

| 任务 | 主要要求 |
|---|---|
| capture | 忠实整理、结构化输出、低延迟 |
| daily_extract | 从聊天中识别用户真实思想，避免过度提取 |
| graph_routing | 稳定判断树位置、更新动作和关系 |
| query | 综合多个节点回答并正确引用 |
| embedding | 中英文和技术术语语义检索 |

管理端需要显示：

- Agent ID。
- 实际 primary 和 fallback。
- 最近一次实际使用模型。
- 健康检查结果。
- 超时和重试配置。

模型不存在、未允许或健康检查失败时，任务明确失败，不静默使用 main default。

---

## 17. Tool 与接口

首批 OpenClaw Tools：

| Tool | 用途 | 权限 |
|---|---|---|
| simai_capture | 主动创建候选思想 | 白名单用户 |
| simai_list_candidates | 查看待确认候选 | 白名单用户 |
| simai_confirm_candidate | 确认、修改或拒绝候选 | 白名单用户 |
| simai_search | 搜索节点 | 白名单用户且数据库已解锁 |
| simai_get_node | 获取节点及关系 | 白名单用户且数据库已解锁 |
| simai_query | 基于思维树问答 | 白名单用户且数据库已解锁 |
| simai_status | 查看锁定、任务和候选数量 | 白名单用户 |

以下能力仅 Web 管理端开放：

- 解锁。
- 修改来源白名单。
- 修改模型和安全设置。
- 批量移动或合并节点。
- 全量导出。
- 备份和恢复。

Tool 返回结果不得包含口令、密钥、内部文件路径或不必要的原始聊天正文。

---

## 18. 导出设计

### 18.1 格式

| 格式 | 用途 |
|---|---|
| simai.json | 无损保存节点、版本、关系和元数据 |
| Markdown | 人工阅读和作为大模型上下文 |
| OPML | 导出主树大纲 |
| GraphML | 导出主树及全部语义关系 |
| JSON Canvas | 导出可视布局、节点和连线 |

OPML 主要表达层级树；GraphML 适合表达节点、边、方向和属性；JSON Canvas 适合表达可视坐标和带标签的边。[OPML 2.0](https://opml.org/spec2.opml)、[GraphML](https://graphml.graphdrawing.org/specification/dtd.html)、[JSON Canvas](https://jsoncanvas.org/spec/1.0/)

### 18.2 导出范围

- 整棵思维树。
- 指定一级主题或子树。
- 手工选择的节点集合。
- 指定时间范围。
- 指定节点类型。
- 只导出用户确认关系，或同时包含 AI 关系。
- 是否包含历史版本。

### 18.3 导出安全

- 只能在 Web 中发起。
- 发起前明确提示导出是否为明文。
- 支持明文临时文件和口令加密包。
- 明文临时文件设置自动删除时间。
- 导出日志只记录范围、时间和文件哈希，不保存导出正文。

---

## 19. 配置示例

~~~yaml
runtime:
  profile: local_wsl
  timezone: Asia/Shanghai

profiles:
  local_wsl:
    openclaw_gateway: http://127.0.0.1:18791
    sources:
      openclaw_web: true
      cli: true
      wechat: false

  tencent_cloud:
    openclaw_gateway: http://127.0.0.1:18789
    sources:
      openclaw_web: false
      cli: true
      wechat: true

source_bindings:
  - id: local_web
    profiles: [local_wsl]
    enabled: true
    channel: webchat
    account_id: local
    sender_key: "<local-owner-id>"
    conversation_id: "<local-test-conversation>"
    binding_key: auto_hmac
    passive_capture: true
    fail_closed_on_missing_identity: true

  - id: yu_weixin
    profiles: [tencent_cloud]
    enabled: false
    channel: openclaw-weixin
    account_id: "<deployment-probe-result>"
    sender_key: "<yu-from-user-id>"
    conversation_id: "<private-conversation-id-if-present>"
    binding_key: auto_hmac
    allow_group: false
    passive_capture: true
    fail_closed_on_missing_identity: true

daily_capture:
  enabled: true
  schedule: "30 22 * * *"
  timezone: Asia/Shanghai
  cutoff_delay_minutes: 30
  process_all_unhandled: true
  review_batch_size: 5
  notify_when_empty: false
  notify_when_locked: true

voice:
  mode: existing_openclaw_pipeline
  transcriber: qwen3.5-omni-plus
  script_path: /home/yu/.openclaw/bin/qwen_omni_transcribe.py
  retain_audio: false

models:
  source: openclaw
  inherit_main_default: false
  task_agents:
    capture: simai
    daily_extract: simai
    graph_routing: simai
    query: simai
  # 可覆盖 OpenClaw 当前 default；Provider 凭据仍由 OpenClaw 管理。
  task_models:
    capture: qwen/qwen3.5-plus
    daily_extract: qwen/qwen3.5-plus

normalization:
  level: light
  preserve_numbers: true
  preserve_negation: true
  preserve_conditions: true
  preserve_uncertainty: true
  personal_dictionary_enabled: true

placement:
  parent_candidates: 3
  auto_commit: false
  allow_auto_merge: false
  allow_auto_move: false
  allow_auto_delete: false

relations:
  auto_generate: true
  max_per_revision: 3
  minimum_confidence: 0.75
  bind_to_revisions: true
  recheck_on_revision: true
  require_confirmation:
    - supersedes
    - branch_move
    - node_merge

storage:
  engine: sqlcipher
  store_chat_transcript: false
  store_audio: false
  store_embeddings_in_database: true
  external_vector_database: false
  encrypted_backup: true

security:
  unlock_mode: web_manual
  key_storage: process_memory
  kdf: argon2id
  root_key_wrap: xchacha20_poly1305
  key_derivation: hkdf
  lock_on_service_restart: true
  lock_on_web_logout: false
  idle_auto_lock_minutes: 0
  allow_unlock_from_wechat: false
  allow_password_in_cli_args: false

sealed_inbox:
  enabled: true
  encryption: public_key_sealed_box
  ingress: unix_socket
  atomic_write: true
  deduplicate_by_message_hmac: true
  retain_after_processing: false
  warning_after_days: 7
  hard_delete_unprocessed: false

web:
  bind: 127.0.0.1
  port: 18880
  unlock_page_enabled: true
  session_idle_minutes: 30
  require_https_when_remote: true

exports:
  formats:
    - simai-json
    - markdown
    - opml
    - graphml
    - json-canvas
  plaintext_ttl_minutes: 30

logging:
  include_message_content: false
  include_candidate_content: false
  include_model_prompt: false
  hash_source_message_ids: true
~~~

以下配置属于安全不变量，不允许通过微信修改：

~~~yaml
voice.retain_audio: false
storage.store_chat_transcript: false
placement.auto_commit: false
placement.allow_auto_merge: false
placement.allow_auto_move: false
placement.allow_auto_delete: false
security.allow_unlock_from_wechat: false
security.allow_password_in_cli_args: false
~~~

---

## 20. 本地 WSL 开发范围

本地阶段不接微信，使用 OpenClaw Web 和 CLI：

~~~text
OpenClaw Web / CLI
→ 思脉 Plugin
→ 思脉核心服务
→ SQLCipher
→ Web 管理端
~~~

首个可用版本必须同时完成：

- SQLCipher 初始化、Web 解锁和手动锁定。
- CLI 文字输入。
- OpenClaw Web 主动记录。
- 本地指定 Web 会话的每日提取测试。
- 内容整理和用户确认。
- 主树归类及节点版本。
- 任意节点间自动关系。
- 待确认箱。
- 树形查看、节点详情和关系视图。
- 关键词及语义搜索。
- 自然语言查询和节点引用。
- Markdown、JSON 和 GraphML 导出。
- 独立 simai Agent 使用非 default 模型。
- 加密备份和恢复。

本地可提供以下命令入口，具体名称在实现时保持一致：

~~~text
simai serve
simai capture
simai daily run
simai tree
simai query
simai export
simai doctor
~~~

---

## 21. 腾讯云部署

本地验收通过后部署同一套代码，不重写核心逻辑。

步骤：

1. 部署思脉 Plugin、核心服务和 Web 静态资源。
2. 通过 systemd 启动处于锁定状态的服务。
3. 通过 SSH Tunnel 打开 Web 解锁页。
4. 将本地 SQLCipher 加密快照迁移到腾讯云。
5. 校验 schema、节点、版本、关系和全文索引。
6. 运行微信元数据探针。
7. 写入精确 Channel、accountId 和 senderKey 白名单。
8. 设置 per-account-channel-peer 会话隔离。
9. 接入现有 Qwen 转写后文字，不修改 ASR 服务。
10. 开启每日 22:30 任务及定向微信提醒。
11. 完成数据库备份、错误口令和恢复演练。

Web 端口不对公网开放。

---

## 22. 备份与恢复

### 22.1 备份

- 使用 SQLCipher 支持的一致性备份方式，不在写入期间直接复制数据库文件。
- 备份目标连接必须先设置 SQLCipher Key，不能假设 Online Backup 自动加密目标。
- 备份生成后验证 cipher_version、cipher_status，并验证错误密钥打开失败。
- sealed inbox 单独保持密文，不产生明文快照。
- 备份写入腾讯云存储前已经是密文。
- 每次备份记录时间、文件哈希、schema 版本和对象数量。

一个可恢复备份必须包含：

~~~text
SQLCipher 加密数据库
密钥头与 KDF 参数
被封装的 Vault Root Key
sealed inbox 密文
被封装的 inbox 私钥
source cursor 与处理回执
schema、版本和文件哈希清单
~~~

离线恢复包不随云端备份复制。

SQLite Online Backup API 可生成一致快照。[SQLite Backup API](https://www.sqlite.org/backup.html)

### 22.2 恢复

每次正式版本至少验证：

1. 正确口令可以恢复。
2. 错误口令无法打开。
3. 数据库完整性检查通过。
4. 节点、版本和关系数量一致。
5. FTS 和 Embedding 可以读取或重建。
6. 未处理 sealed inbox 和 source cursor 能够恢复。
7. 恢复后定时任务不会丢失或重复生成候选。

---

## 23. 日志、监控与故障处理

允许记录：

- 任务开始、结束和耗时。
- 处理消息数量。
- 生成、确认和拒绝候选数量。
- 节点及关系数量。
- 模型名称、延迟、重试和错误类型。
- 加密后的来源 ID 或 HMAC。

禁止记录：

- 用户消息正文。
- 候选和节点正文。
- 模型完整 prompt 和回答。
- 用户口令、数据库密钥和 Provider Key。
- 明文导出内容。

关键告警：

- 服务或数据库处于锁定状态。
- 每日任务连续失败。
- 模型连续失败或实际模型不符合配置。
- sealed inbox 超过保留期限。
- 备份或恢复校验失败。
- 来源身份字段发生变化。

---

## 24. 验收标准

### 24.1 功能

- 主动文字可以生成候选并确认写入。
- 模拟转写文字可以走完与微信语音相同的后续流程。
- 普通聊天每天只处理一次增量。
- 树、节点版本和语义关系均可查看。
- 支持关键词、语义和自然语言查询。
- 支持至少 Markdown、simai.json 和 GraphML 导出。

### 24.2 准确性

- AI 回答被保存为用户思想的数量为 0。
- 数字、否定、条件和不确定性不能在整理中丢失。
- 每个正式节点只有一个主父节点。
- 重复任务不产生重复候选。
- 查询回答可以引用具体节点和版本。

### 24.3 Channel 隔离

- 非白名单 Channel 进入思脉的消息数量为 0。
- 非指定 accountId 或 senderKey 的消息数量为 0。
- 群聊消息数量为 0。
- 每日提醒只发往配置的精确微信身份。

### 24.4 隐私与安全

- 思脉保存的语音文件数量为 0。
- 思脉保存的完整聊天记录数量为 0。
- 候选确认或拒绝后，source_excerpt_ciphertext 遗留数量为 0。
- 普通 SQLite 和错误口令无法打开数据库或备份。
- 服务重启后数据库保持锁定。
- 微信无法执行解锁。
- 日志中不存在思想正文和口令。
- Web 管理端默认不监听公网地址。

### 24.5 模型

- simai 使用独立 Agent，不强制使用 main default。
- 管理端可以确认实际 primary 和 fallback。
- 模型不可用时明确失败，不静默切换为未配置模型。

---

## 25. 实施阶段

### Phase 0：OpenClaw 接口确认

- 确认现有 Qwen 脚本在 OpenClaw 中的挂载位置。
- 验证 ASR 后最终文字对应的 Hook。
- 验证微信消息实际提供的身份字段。
- 确认本地与腾讯云 OpenClaw 版本差异。
- 确认 simai Agent 的模型调用方式。

### Phase 1：本地存储与核心流程

- SQLCipher、密钥封装和 Web 解锁。
- sealed inbox。
- 节点、版本、候选、关系和审计表。
- CLI 主动记录及确认。
- 内容整理、归类和关系生成。

### Phase 2：本地 Web 与查询

- 待确认箱。
- 树形和节点详情。
- 关系视图。
- 搜索和自然语言查询。
- 配置、导出、备份和恢复。

### Phase 3：本地 OpenClaw 集成

- Plugin 和 Tools。
- OpenClaw Web 主动记录。
- 指定 Web 会话每日提取。
- 独立 simai Agent 与非 default 模型。
- 端到端验收。

### Phase 4：腾讯云与微信

- systemd 部署。
- 精确微信来源绑定。
- 复用现有 Qwen 语音流程。
- 每日定时和微信提醒。
- SSH Tunnel 解锁。
- 迁移、备份和恢复演练。

---

## 26. 风险与控制

| 风险 | 控制 |
|---|---|
| 模型过度提取普通聊天 | 严格候选标准、每日批量确认、可调提取阈值 |
| 相似主题被错误合并 | 禁止自动合并，提供 3 个候选位置 |
| 树分支过多 | 允许建议补充和修正，但仍由用户确认 |
| 自动关系过多导致图混乱 | 每版本最多 3 条、置信度阈值、默认局部关系图 |
| 节点修改后关系失真 | 关系绑定节点版本，修改后进入 stale |
| 多 Channel 串线 | channel + accountId + senderKey 精确白名单 |
| 服务重启期间消息丢失 | Plugin 使用公钥写 sealed inbox |
| 口令进入微信或模型日志 | 只允许 Web 解锁，禁止微信和 CLI 参数传入口令 |
| 后台模型意外使用 default | 独立 simai Agent、启动检查和实际模型审计 |
| 导出造成明文泄露 | Web 显式确认、临时文件 TTL、支持加密导出 |
| OpenClaw 或模型服务保留数据 | 在产品中明确安全边界，敏感任务可选择本地模型 |

---

## 27. 后续能力

不进入首个 MVP，但数据结构需允许后续扩展：

- 周报、月报式思想演化摘要。
- 自动发现长期矛盾、盲区和未验证假设。
- 按项目或角色建立不同局部视图。
- 向其他大模型提供只读 MCP。
- 本地模型优先的隐私模式。
- 加密移动端 Web。
- 基于用户确认记录优化归类和关系判断。
- 选择性导入其他个人笔记，但仍要求确认后进入主树。

---

## 28. 最终决策汇总

| 项目 | 决策 |
|---|---|
| 项目名称 | 思脉（Simai） |
| 主数据结构 | 唯一主路径树 + 任意语义关系 |
| 主数据库 | SQLCipher SQLite |
| 思维导图格式 | 只用于导出，不作为主存储 |
| OpenClaw 形态 | Plugin + Tool + 核心服务，MCP 后续可选 |
| 语音 | 完全复用现有 Qwen3.5-Omni-Plus 链路 |
| 音频保留 | 不保留 |
| 普通聊天观察 | 默认开启 |
| 每日整理 | 22:30，Asia/Shanghai |
| 对话来源 | 精确绑定微信 Channel、账号和 Yu 的发送者 ID |
| 聊天记录 | 由 OpenClaw 维护，思脉不复制完整历史 |
| 正式写入 | 必须用户确认 |
| 语义关系 | 自动生成并记录属性，明确标记 AI 来源 |
| 查看与查询 | 与核心树功能同时开发 |
| 模型 | 独立 simai Agent，可配置非 default 模型 |
| 解锁 | 后台 Web 人工输入口令 |
| 微信解锁 | 禁止 |
| 运行中密钥 | 只保存在进程内存 |
| 锁定期间采集 | 公钥加密 sealed inbox |
| 首发环境 | 本地 WSL，OpenClaw Web/CLI |
| 正式环境 | 腾讯云 + 指定微信 OpenClaw Channel |

---

## 29. 参考资料

- [OpenClaw Plugin Hooks](https://docs.openclaw.ai/plugins/hooks)
- [OpenClaw v2026.7.1 Internal Hooks](https://github.com/openclaw/openclaw/blob/v2026.7.1/docs/automation/hooks.md)
- [OpenClaw v2026.7.1 Audio Flow](https://github.com/openclaw/openclaw/blob/v2026.7.1/docs/nodes/audio.md)
- [OpenClaw Automations](https://docs.openclaw.ai/automation/cron-jobs)
- [OpenClaw Session Tools](https://docs.openclaw.ai/concepts/session-tool)
- [OpenClaw Channel Routing](https://docs.openclaw.ai/channels/channel-routing)
- [Tencent openclaw-weixin](https://github.com/Tencent/openclaw-weixin)
- [SQLite Recursive CTE](https://www.sqlite.org/lang_with.html)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [SQLite Backup API](https://www.sqlite.org/backup.html)
- [SQLCipher Design](https://www.zetetic.net/sqlcipher/design/)
- [OPML 2.0](https://opml.org/spec2.opml)
- [GraphML](https://graphml.graphdrawing.org/specification/dtd.html)
- [JSON Canvas](https://jsoncanvas.org/spec/1.0/)
- [XMind AI](https://xmind.com/ai)
- [Obsidian](https://obsidian.md/)
- [Heptabase](https://heptabase.com/)
- [Plaud Intelligence](https://www.plaud.ai/pages/plaud-intelligence)
