# 思脉 OpenClaw Plugin

适配 OpenClaw `2026.7.1` 插件 API。插件监听已绑定来源的用户消息，将
`message_received(event, ctx)` 的身份与 `message:preprocessed` 的最终文本关联，
然后送入思脉加密待确认箱。它不接管微信语音：现有
`微信语音 → 自定义脚本 → Qwen3.5-Omni-Plus → 当前聊天模型` 流程保持不变。

## 构建与安装

~~~bash
cd /opt/simai/plugin
npm ci
npm run build
openclaw plugins install /opt/simai/plugin
~~~

`openclaw.plugin.json` 声明配置 Schema 和全部 `simai_*` 工具，`package.json`
同时声明源码入口和构建后的 runtime 入口。最低宿主/API 版本均为 `2026.7.1`。

## OpenClaw 配置

把下列内容放到 `plugins.entries.simai.config`。路径请使用部署机器上的绝对路径：

~~~json5
{
  plugins: {
    entries: {
      simai: {
        enabled: true,
        config: {
          coreUrl: "http://127.0.0.1:18880",
          coreTokenFile: "/var/lib/simai/plugin.token",
          inboxSocket: "/var/lib/simai/inbox.sock",
          vaultHeaderPath: "/var/lib/simai/vault.header.json",
          inboxDir: "/var/lib/simai/inbox",
          correlationWindowMs: 300000,
          bindings: [
            {
              id: "yu_weixin",
              channel: "openclaw-weixin",
              accountId: "<微信 Channel accountId>",
              senderKey: "<Yu 的 senderId/from>",
              conversationId: "<私聊 conversationId>",
              allowGroup: false,
              passiveCapture: true,
              enabled: true
            }
          ]
        }
      }
    }
  }
}
~~~

部署新环境时可先在 config 中加 `probeMode: true`（binding 用占位值并保持
`enabled: false`）：此模式完全停用捕获，仅将每条消息的身份元数据
（channel/account/sender/conversation/messageId、sessionKey 有无、正文长度与
是否媒体占位符）以 `simai[probe]` 前缀写入日志，绝不记录正文。抄下真实字段值
填入 binding 后关闭 probeMode。

同一个 binding 的 `id/channel/accountId/senderKey/conversationId/allowGroup` 必须与
思脉 YAML 中的 `source_bindings` 完全一致。插件与 Python ingress 都会核验身份；
未知来源、歧义配置、群聊或关联字段冲突一律拒绝。

仅所有者可访问的本地通道（如 loopback Dashboard 的 `webchat`）不携带任何
account/sender 身份字段。对这类通道可在 binding 上显式设置
`allowMissingIdentity: true`：当载荷完全缺失身份字段时，用 binding 配置的身份
填充信封（Python 侧核验不变）；载荷中任何存在但不匹配的字段仍然拒绝。
同一通道只允许一个此类 binding，歧义时不捕获。严禁对微信等真实多用户通道启用。`sessionKey` 只用于同一轮工具
授权；预处理 hook 没有 sessionKey 时，仍可通过 messageId 和完整消息身份关联。

捕获行为：`/` 开头的宿主命令（如 `/reset`）在任何模式下都不会入箱。速记
（驾驶）模式用独立消息触发——开启：「我在开车，接下来只记录」「进入速记模式」
「开始记录」「我有想法请记录」；关闭：「不开车了」「结束速记模式」「记录完毕」
「停止记录」。期间所有消息按 `explicit` 逐字入箱，跳过日批的保守聊天过滤；
同一消息在关联窗口内只密封一次。每次开启速记会生成一个 `dictation_id` 随
信封携带；速记期间助手已送达的回复也会以 `speaker: assistant` 入箱（通过
`message_sent` hook，同一 `dictation_id`），作为合并时的上下文。核心日批用
`dictation_merge` 模型任务整理整个会话：默认合并成**一条**主题候选；只有
当主人的输入有明确编号（1. 2. / 第一…第二…）且内容确实不相关时才拆成多条；
助手的内容只有被主人明确肯定或评论过才并入主题正文（标注「采纳自助手回复」），
未被回应的建议一律丢弃。模型故障时回退为主人原话的逐字合并，显式记录永不丢失。

「结束记录」是权威的会话完结信号：插件会立即通知核心
（`POST /plugin-api/dictation/close`），已关闭的会话**跳过日批的 30 分钟
静默窗**，解锁状态下几秒内即出合并候选；锁定状态下解锁补跑时同样豁免。
通知失败无害——未收到关闭信号的会话回退为"全部消息静默满窗口后整体处理"
（会话级原子性，长会话不会被劈成两批）。

正常写入走 0600 Unix Socket。Socket/Core 短暂不可用时，插件读取
`vault.header.json` 中的公开收件箱密钥，以 libsodium sealed box 生成 schema v2
密文并以 0600 原子写入 `inboxDir`；不会建立明文 spool。服务明确返回身份拒绝时
不会绕过拒绝直接写文件。

## 22:30 command cron

插件 API 没有 `registerCron` 或通用 `sendMessage`，因此定时任务由运维者显式创建。
以下任务直接调用无模型的 Worker，输出只含状态/数量，不含思想正文：

~~~bash
openclaw cron add \
  --name "simai-daily-yu" \
  --cron "30 22 * * *" \
  --tz "Asia/Shanghai" \
  --command-argv '["node","/opt/simai/plugin/dist/daily-command.js","--core-url","http://127.0.0.1:18880","--token-file","/var/lib/simai/plugin.token","--binding-id","yu_weixin"]' \
  --announce \
  --channel openclaw-weixin \
  --account "<微信 Channel accountId>" \
  --to "<Yu 的精确接收 ID>"
~~~

不要使用 `last`、通配符或最近会话作为生产投递目标。无候选时 Worker 输出
`NO_REPLY`，OpenClaw 不发送提醒；Vault 锁定时只提示去 Web 管理端解锁。

验证任务：

~~~bash
openclaw cron list
openclaw cron run <job-id> --wait
openclaw cron runs --id <job-id> --limit 5
~~~
