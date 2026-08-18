# 腾讯云部署 · 阶段二：微信被动采集

前提：阶段一（Web/CLI 主动记录）已在云上验收通过。本文档把
`REVIEW_AND_CHANGES.md` 第 7 节的 10 条上线门槛展开成可执行命令，
按顺序做完并勾选，最后才把 `yu_weixin` 绑定置为 `enabled: true`。

约定（按你的实际环境替换）：

- 云上项目目录：`~/workspace/simai`，Python venv：`mydev`
- OpenClaw Gateway：`127.0.0.1:18789`（systemd user 服务，带 token 认证）
- Simai 数据目录：`~/.simai`（`plugin.token`、`inbox.sock`、`vault.header.json`、`inbox/` 都在这里，
  `plugin.token` 由 `simai serve` 首次启动时自动生成）
- Simai Web 端口：以 `~/.simai/simai.yaml` 的 `web.port` 为准（下文用 `18880` 示例）

---

## 第 1 步：同步并安装 Plugin（门槛 1）

本地（WSL）已完成构建与 16 项测试，`git push` 后在云上拉取。
注意 `plugin/dist/` 不进 git，云端需要自己构建（锁文件已含 typescript）：

~~~bash
cd ~/workspace/simai && git pull
cd plugin
npm ci --registry=https://registry.npmmirror.com   # 含 dev 依赖，构建需要
npm run build                                       # 生成 dist/
npm prune --omit=dev                                # 可选：构建后移除 dev 依赖
openclaw plugins install ~/workspace/simai/plugin
~~~

先给插件一个**探针配置**（probeMode 开、绑定占位且 disabled，不会采集任何内容）。
编辑 `~/.openclaw/openclaw.json`，在 `plugins.entries` 增加：

~~~json5
simai: {
  enabled: true,
  config: {
    coreUrl: "http://127.0.0.1:18880",
    coreTokenFile: "/home/yu/.simai/plugin.token",
    inboxSocket: "/home/yu/.simai/inbox.sock",
    vaultHeaderPath: "/home/yu/.simai/vault.header.json",
    inboxDir: "/home/yu/.simai/inbox",
    probeMode: true,
    "bindings": [
      {
        "id": "yu_weixin",
        "channel": "openclaw-weixin",
        "accountId": "66278cd41480-im-bot",
        "senderKey": "o9cq8058R7wmTrx-3gQk-4GK_FX4@im.wechat",
        "conversationId": "o9cq8058R7wmTrx-3gQk-4GK_FX4@im.wechat",
        "allowGroup": false,
        "passiveCapture": true,
        "enabled": true
      }
    ]
  }
}
~~~

重启 Gateway 并确认加载：

~~~bash
systemctl --user restart openclaw-gateway
sleep 5
grep -a "simai plugin registered" /tmp/openclaw/openclaw-$(date +%F).log | tail -2
~~~

**验收 1 通过标准**：日志出现
`simai plugin registered mode=full bindings=0 probeMode=on (capture disabled, metadata-only logging)`，
且启动日志无 manifest/Hook/Tool 报错。

## 第 2 步：payload probe（门槛 2）

保持 probeMode 开启。用你的微信向 Bot 微信号发一条**文字**（内容随意，
探针不会记录正文，只记录长度）。然后：

~~~bash
grep -a "simai\[probe\]" /tmp/openclaw/openclaw-$(date +%F).log | tail -10
~~~

每条消息会有两行 JSON（`message_received` 和 `message_preprocessed`），抄下：

| 字段 | 用途 |
|---|---|
| `channelId` | 应为 `openclaw-weixin`，填 binding `channel` |
| `accountId` | 填 binding `accountId` |
| `senderId` / `from` | Yu 的发送者标识，填 binding `senderKey`（以 `senderId` 为准，为空再用 `from`） |
| `conversationId` | 私聊会话 ID，填 binding `conversationId`（若为 null 则 binding 里省略该项） |
| `messageId` / `hasSessionKey` | 确认两个 hook 都携带，能完成两阶段关联 |

**验收 2 通过标准**：两行探针日志字段齐全；日志中不出现你发送的正文。

## 第 3 步：语音链路确认（门槛 4 前半）

仍在 probeMode 下，发一条**语音**：

~~~bash
grep -a "simai\[probe\] message_preprocessed" /tmp/openclaw/openclaw-$(date +%F).log | tail -2
~~~

**通过标准**：`bodyLooksLikeMediaPlaceholder: false` 且 `bodyLength` 与转写文字长度相称
（比如说了 20 个字，长度应在几十而不是 7——`[Audio]` 占位是 7）。
若为 `true`，说明 ASR（Qwen3.5-Omni-Plus）不在媒体预处理链上，**停止**，先修 ASR 配置。

顺带发一条**别人的消息**（用家人/朋友微信发一句）和一条**群聊消息**，
记下它们的 `senderId`/`isGroup`，供第 6 步负向测试对照。

## 第 4 步：dmScope 设置（门槛 3 前置）

~~~bash
openclaw config set session.dmScope per-account-channel-peer
systemctl --user restart openclaw-gateway
openclaw config get session.dmScope
~~~

## 第 5 步：填入真实绑定，关闭探针

两处必须完全一致（插件端与 Core 端双重核验，任何不一致都会拒收）：

1. `~/.openclaw/openclaw.json` → `plugins.entries.simai.config`：
   - `probeMode` 删除或改为 `false`
   - binding 填入第 2 步抄下的真实值，`enabled: true`
2. `~/.simai/simai.yaml` → `source_bindings` 里的 `yu_weixin`：
   - `account_id` / `sender_key` / `conversation_id` 填同样的值
   - `enabled: true`；确认 `profiles.tencent_cloud.sources.wechat: true`

然后重启两边：

~~~bash
systemctl --user restart openclaw-gateway
systemctl --user restart simai   # 或你运行 simai serve 的方式
grep -a "simai plugin registered" /tmp/openclaw/openclaw-$(date +%F).log | tail -1
# 期望：bindings=1，且无 probeMode 字样
~~~

## 第 6 步：正向 + 驾驶（速记）模式验证（门槛 5）

> 触发短语必须是独立的一条消息。开启：「我在开车，接下来只记录」「进入速记模式」
> 「开始记录」「我有想法请记录」；关闭：「不开车了」「结束速记模式」「记录完毕」
> 「停止记录」。期间所有消息按 explicit 逐字入箱，不经过日批的保守聊天过滤。
> 另外 `/` 开头的宿主命令（如 `/reset`）在任何模式下都不会入箱。
> 同一次速记会话（开启→关闭之间）共享一个 `dictation_id`，Bot 在期间的回复也
> 以 `speaker=assistant` 同会话入箱（仅作上下文）。日批用 `dictation_merge`
> 模型整理整个会话：默认**合并成一条主题候选**；只有当输入带明确编号
> （1. 2. / 第一、第二）且内容确实不相关时才拆成多条；Bot 回复中只有被你
> 明确肯定或评论过的要点才并入主题（标注「采纳自助手回复」）。模型故障时
> 回退为你的原话逐字合并，显式记录永不丢失。

1. 微信发「记录一下：<一句想法>」→ Bot 应回确认卡（`simai_capture` 工具）。
2. 微信依次发：「我在开车，接下来只记录」→ 再发三条围绕同一主题的想法
   →（若 Bot 有回复，可对其中一条回「对，就按这个」）→ 「不开车了」。
3. 「不开车了/结束记录」即宣告会话完结：插件通知核心后**跳过 30 分钟静默窗**。
   若 Web 已解锁，几秒~几十秒内待确认箱直接出现**一条**合并候选（三条想法按
   说出顺序整合；若第 2 步做了肯定回复，候选正文还应包含「采纳自助手回复：…」）。
   若当时锁定，解锁时自动补跑，同样豁免静默窗。验证关闭信号送达：
   `grep -ao 'simai[^"\\]*dictation close notified[^"\\]*' /tmp/openclaw/openclaw-$(date +%F).log | tail -2`
4. 可选拆分验证：再开一次速记，发「1. <想法A>」「2. <一件不相关的事>」后关闭；
   应产出**两条**独立候选。

> 静默窗仅对"未正常关闭"的会话生效（如插件重启丢了关闭消息）：这类会话等
> 全部消息静默满 `cutoff_delay_minutes` 后整体处理，长会话不会被劈成两半。
> 普通被动消息仍走静默窗，防止截断进行中的对话。

## 第 7 步：锁定态密文验证（门槛 6）

~~~bash
# Web 上锁定 Vault 后，用微信发一条想法，然后：
ls -l ~/.simai/inbox/          # 只应有 0600 的 *.sealed 文件
file ~/.simai/inbox/*.sealed   # data（密文），cat 不出可读内容
cp ~/.simai/inbox/*.sealed /tmp/steal-test && cat /tmp/steal-test  # 复制后同样不可读
rm /tmp/steal-test
~~~

Web 解锁后，确认这条消息进入待确认流程（每日整理或立即出现）。

## 第 8 步：command cron（门槛 7）

~~~bash
openclaw cron add \
  --name "simai-daily-yu" \
  --cron "30 22 * * *" \
  --tz "Asia/Shanghai" \
  --command-argv '["node","/home/yu/workspace/simai/plugin/dist/daily-command.js","--core-url","http://127.0.0.1:18880","--token-file","/home/yu/.simai/plugin.token","--binding-id","yu_weixin"]' \
  --announce \
  --channel openclaw-weixin \
  --account "<第2步的 accountId>" \
  --to "<第2步的 conversationId 或你的精确接收 ID>"

openclaw cron list
openclaw cron run <job-id> --wait
openclaw cron runs --id <job-id> --limit 5
~~~

**通过标准**：有未处理消息时提醒精确投递到指定 account/to；清空后再跑一次输出
`NO_REPLY` 且微信不收到消息。

## 第 9 步：负向测试（门槛 3 后半 + 门槛 8）

逐项操作，**每项之后**检查 `~/.simai/inbox/` 无新增文件、Web 待确认箱无新候选：

1. 其他发送者私聊 Bot（第 3 步用过的家人/朋友微信）。
2. 群聊里 @Bot 或直接发言（包括你自己在群里发）。
3. 若有第二个微信 Channel/账号，向其发消息。
4. 临时把 `~/.openclaw/openclaw.json` 里 binding 的 `conversationId` 改成错误值，
   重启 Gateway，自己发一条 → 应被拒收；改回后恢复。

同时看拒收日志确认是身份校验在起作用：

~~~bash
grep -ia "simai" /tmp/openclaw/openclaw-$(date +%F).log | grep -iav probe | tail -20
~~~

## 第 10 步：备份演练（门槛 9）

~~~bash
simai backup --output ~/simai-backups/
# 离线复制到本地（本地执行）：
scp 云主机:~/simai-backups/<备份文件> ~/Downloads/
# 云上：错误口令必须失败，正确口令恢复并核对对象数
simai restore <备份文件> --target /tmp/restore-test   # 先输错口令一次，再输对
simai tree   # 对比恢复库与线上库的节点数
rm -rf /tmp/restore-test
~~~

## 第 11 步：日志审计（门槛 10）

~~~bash
grep -a "<你在第6步发的某句想法关键词>" /tmp/openclaw/openclaw-*.log || echo "OK: 正文未落日志"
journalctl --user -u simai --since today | grep -i -e passphrase -e token || echo "OK"
grep -ia "x-simai-plugin-token\|gateway.token" /tmp/openclaw/openclaw-*.log || echo "OK"
~~~

**通过标准**：OpenClaw、systemd、Uvicorn 日志中无思想正文、搜索词、口令、Token、
Prompt 或模型输出（Simai 默认关闭 access log，模型调用日志只含任务名与模型 ID）。

---

## 最终检查清单

- [ ] 1. 插件在云上 OpenClaw 加载无契约错误
- [ ] 2. probe 锁定全部身份字段，日志无正文
- [ ] 3. dmScope=per-account-channel-peer，且负向测试全部拒收
- [ ] 4. 语音 `bodyForAgent` 是真实转写而非 `[Audio]`
- [ ] 5. 主动记录返回确认卡；驾驶模式三条全部 explicit 入箱
- [ ] 6. 锁定态只有 0600 密文，复制不可读，解锁后可补处理
- [ ] 7. cron 手工运行行为正确，NO_REPLY 不打扰
- [ ] 8. 负向测试（他人/群聊/他号/错 conversationId）全部拒绝
- [ ] 9. 备份→离线复制→错误口令失败→正确恢复→对象数一致
- [ ] 10. 全部日志无敏感内容

全部勾选后，微信被动采集即为正式启用状态。

---

## 附：本轮功能更新的云端升级步骤

1. 速记会话合并：`git pull` 后**必须同时**更新插件与核心——
   `cd plugin && npm ci && npm run build && openclaw plugins install --force ~/workspace/simai/plugin`，
   修正安装目录权限（`find ~/.openclaw/extensions/simai -type d -exec chmod 755 {} +; find ~/.openclaw/extensions/simai -type f -exec chmod 644 {} +`），
   重启 Gateway；再重启 `simai serve`。旧插件写入的密文仍可正常解密（按无会话处理）。
   合并质量取决于 `dictation_merge` 模型任务（默认复用 `daily_extract` 的
   Agent）；要用更强的模型，在 `~/.simai/simai.yaml` 的 `models` 下加
   `task_agents.dictation_merge: simai` 与 `task_models.dictation_merge: <强模型标识>`。
   验证助手回复入箱：速记期间 Bot 回复后，
   `grep -ao 'simai[^"\\]*assistant reply sealed[^"\\]*' /tmp/openclaw/openclaw-$(date +%F).log | tail -2`。
   「结束记录」会立即触发该会话的合并处理（跳过 30 分钟静默窗）；此功能同样
   要求插件与核心同时更新到本轮版本。
2. Web「AI 整理子节点」：节点详情页与思维树页新增按钮，分析结果只产生
   **待确认**的合并建议（待确认箱）与 `ai_generated` 关系（节点详情页确认/否决）。
   整理任务默认复用 `daily_extract` 的 Agent；要用更强的模型，在
   `~/.simai/simai.yaml` 的 `models` 下加：
   `task_agents.reorganize: simai` 与 `task_models.reorganize: <强模型标识>`
   （经 `x-openclaw-model` 转发，无需在 Simai 存任何 API key）。
