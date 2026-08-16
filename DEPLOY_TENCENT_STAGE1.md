# 腾讯云部署 · 阶段一（Web/CLI 主动记录）

范围：只启用 Web 与 CLI 主动记录。**不安装 Plugin、不开微信被动采集**——那是阶段二，
必须先通过 `REVIEW_AND_CHANGES.md` 第 7 节的 10 项实机验收。

前提：腾讯云服务器上已有可用的 OpenClaw（含你配置好的模型 Provider）。

---

## 第 1 步 · 打包上传代码（在 WSL 本地执行）

```bash
cd ~/workspace
tar czf simai-deploy.tar.gz \
  --exclude='simai/.git' --exclude='*__pycache__*' --exclude='*.egg-info' \
  --exclude='simai/plugin/node_modules' --exclude='simai/plugin/dist' \
  simai
scp simai-deploy.tar.gz <user>@<腾讯云IP>:~/
```

## 第 2 步 · 服务器基础环境（以下均在服务器上执行）

建议用独立的 Linux 用户运行思脉（与 OpenClaw 同用户也可接受，但不要用 root）。

```bash
tar xzf ~/simai-deploy.tar.gz -C ~/workspace/   # 没有 ~/workspace 先 mkdir
python3.12 --version                            # 需要 3.12；没有则先安装
python3.12 -m venv ~/.venvs/simai
~/.venvs/simai/bin/pip install -e '~/workspace/simai[dev]' 2>/dev/null \
  || ( cd ~/workspace/simai && ~/.venvs/simai/bin/pip install -e '.[dev]' )
# 让 simai 命令进 PATH（或直接用 ~/.venvs/simai/bin/simai）
echo 'export PATH="$HOME/.venvs/simai/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc

# 不联外网跑一遍测试，确认环境完好
cd ~/workspace/simai && python tests/smoke_test.py && pytest -q
```

## 第 3 步 · OpenClaw 侧：创建 simai 专用 Agent

```bash
# 3.1 确认 Gateway 在跑、记下端口（下文以 18789 为例，以实际为准）
openclaw gateway status         # 或查看你的启动配置

# 3.2 创建专用 agent（模型选云上可用的；工作区放 agents 子目录避免污染）
mkdir -p ~/workspace/agents/simai_workspace
openclaw agents add simai \
  --workspace ~/workspace/agents/simai_workspace \
  --model deepseek/deepseek-v4-flash        # ← 换成云上实际可用的模型

# 3.3 给工作区一个最小 AGENTS.md（避免 WorkspaceVanished 保护误触发，
#     也避免默认 persona 文件白白吃 prompt token）
cat > ~/workspace/agents/simai_workspace/AGENTS.md <<'EOF'
# simai agent
此 Agent 仅供思脉（Simai）通过 Gateway HTTP API 调用，用于文本整理、
归类路由、问答与 embedding。不主动发送消息，不使用工具。
EOF
```

编辑 `~/.openclaw/openclaw.json`，在 `agents.list` 中找到 `simai` 条目，
加上本地 embedding 配置（gemma 300M，纯 CPU，单条 2~3 秒）：

```json
"memorySearch": {
  "enabled": true,
  "provider": "local",
  "fallback": "none",
  "model": "embeddinggemma-300m"
}
```

然后验证（首次 embedding 调用会下载 gemma 模型，稍等）：

```bash
openclaw gateway restart    # 或你的重启方式
curl -s http://127.0.0.1:18789/v1/models | grep simai   # 应看到 openclaw/simai

curl -s -X POST http://127.0.0.1:18789/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"openclaw/simai","messages":[{"role":"user","content":"回复一个字：好"}],"max_tokens":512}'

curl -s -X POST http://127.0.0.1:18789/v1/embeddings \
  -H 'Content-Type: application/json' -H 'x-openclaw-model: embeddinggemma-300m' \
  -d '{"model":"openclaw/simai","input":"测试"}' | head -c 200   # 应返回 768 维向量
```

如 Gateway 开启了鉴权，把 token 存到文件并收紧权限：
`install -m 600 /dev/null ~/.simai/gateway.token && echo '<token>' > ~/.simai/gateway.token`

## 第 4 步 · 思脉配置

```bash
mkdir -p ~/.simai && chmod 700 ~/.simai
cp ~/workspace/simai/config/simai.yaml ~/.simai/simai.yaml
chmod 600 ~/.simai/simai.yaml
vi ~/.simai/simai.yaml
```

必改项：

| 位置 | 改成 |
|---|---|
| `runtime.profile` | `tencent_cloud` |
| `profiles.tencent_cloud.openclaw_gateway` | 实际 Gateway 端口，如 `http://127.0.0.1:18789` |
| `profiles.tencent_cloud.sources.wechat` | **`false`**（阶段一不开被动采集） |
| `models.gateway_token_file` | Gateway 有鉴权则填 `~/.simai/gateway.token`，否则保持 `null` |

保持不动：`task_agents` 全部指向 `simai`；`embedding_model: embeddinggemma-300m`；
`yu_weixin` binding 保持 `enabled: false`（占位符身份，代码也会拒绝启用）。
`source_bindings` 里的 `local_cli` 只在 `local_wsl` profile 生效，如需云上 CLI
记录，把它的 `profiles` 加上 `tencent_cloud`。

## 第 5 步 · 初始化与冒烟

```bash
simai doctor        # 检查 SQLCipher / 配置 / Gateway 连通
simai init          # 设置口令；屏幕上的一次性恢复包立即保存到离线介质！
                    # 恢复包 + 口令都丢失 = 数据设计上不可恢复

# CLI 冒烟（口令交互输入）
printf '%s\n' '云上第一条：部署完成后先小规模试用一周' | simai capture
simai tree
printf '%s\n' '我最近记录了什么？' | simai query
```

## 第 6 步 · 启动 Web 服务（systemd 常驻）

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/simai.service <<'EOF'
[Unit]
Description=Simai web service
After=network.target

[Service]
ExecStart=%h/.venvs/simai/bin/simai serve
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now simai
loginctl enable-linger $USER      # 让服务在退出 SSH 后继续运行
systemctl --user status simai
```

注意：`lock_on_service_restart: true`，服务重启后必须在 Web 上重新输口令解锁。
保持单 Worker，不要横向扩展（进程内互斥锁假设）。

## 第 7 步 · SSH Tunnel 访问（在你本地机器执行）

Web 只监听 loopback（127.0.0.1:18880），**不要**开安全组端口、不要配公网反代。

```bash
ssh -N -L 18880:127.0.0.1:18880 <user>@<腾讯云IP>
# 浏览器打开 http://127.0.0.1:18880，输入口令解锁
```

## 第 8 步 · 上线核对清单

- [ ] Web「配置与状态」→ 测试模型健康：5 个任务全部「正常」
- [ ] Web 记录一条 → 确认写入 → 树中可见 → 问答能引用它
- [ ] `ss -tlnp | grep 18880` 只绑定 127.0.0.1；安全组未开放 18880/18789
- [ ] `ls -la ~/.simai`：目录 0700，`vault.header.json`、`simai.db` 等为 0600
- [ ] Web 创建加密备份成功；把备份目录拷到别机验证无口令打不开
- [ ] 锁定 → 重新解锁流程正常
- [ ] OpenClaw / systemd 日志里没有思想正文、口令、token（思脉日志默认不含正文）
- [ ] 未安装 plugin、`wechat: false`、`yu_weixin` 仍为 disabled

## 故障速查

| 症状 | 原因与处理 |
|---|---|
| 一切请求 HTTP 401 | Gateway 开了鉴权：从 `openclaw.json` 取 token 存入 `~/.simai/gateway.token`（0600），curl 加 `Authorization: Bearer`，`simai.yaml` 配 `gateway_token_file`（默认是 `null`，别忘了改）并重启 serve |
| 健康检查 HTTP 400 | `openclaw/simai` 不存在：agent 未创建或 Gateway 未重启 |
| 健康检查 HTTP 429 | 云上该模型配额用尽：换 `openclaw agents` 里 simai 的 model |
| embedding HTTP 400 | agent 的 `memorySearch.provider` 不是 `local`，或模型名不符 |
| embedding 500 报 `Unknown memory embedding provider: local` | 缺 llama-cpp 插件：`openclaw plugins install llama-cpp` 后重启 Gateway |
| embedding 500 报 `Cannot find module 'node-llama-cpp'` | 插件的嵌套依赖没装成：进 `~/.openclaw/npm/projects/openclaw-llama-cpp-provider-*/node_modules/@openclaw/llama-cpp-provider` 跑 `npm install node-llama-cpp --registry=https://registry.npmmirror.com`（裸 `npm install` 会报 `workspace:*` 错误，忽略即可）；用 `node -e "require.resolve('node-llama-cpp')"` 在该目录验证 |
| embedding 首次调用无限挂起（无 CPU、无网络连接） | gemma GGUF 下载不动（HF 被墙）：从已有机器拷 `~/.node-llama-cpp/models/hf_ggml-org_embeddinggemma-300m-qat-Q8_0.gguf` 到相同路径（328,577,056 字节，scp 后 sha256 校验），重启 Gateway |
| Gateway HTTP 500 WorkspaceVanished | agent 工作区被清空：恢复最小 `AGENTS.md` |
| `simai doctor` 报 SQLCipher | wheel 未装好：`pip install --force-reinstall sqlcipher3-wheels` |

---

阶段二（微信被动采集）入口：`REVIEW_AND_CHANGES.md` 第 7 节 + `plugin/README.md`。
从 payload probe（只记元数据）开始，验收全过之前不启用 `yu_weixin` binding。
