"""Simai CLI (section 20).

Commands: serve, init, capture, daily run, tree, query, export, doctor,
backup, restore, lock-check.

Security: the passphrase is always read interactively (getpass) - it is
never accepted as a command-line argument (security invariant
`allow_password_in_cli_args: false`).
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import sys
from pathlib import Path

import typer

from .config import load_config
from .core.state import AppState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

app = typer.Typer(help="思脉（Simai）- 持续生长的个人思想树", no_args_is_help=True)
daily_app = typer.Typer(help="每日提取任务")
app.add_typer(daily_app, name="daily")

CONFIG_OPT = typer.Option(None, "--config", "-c", help="配置文件路径（默认 ~/.simai/simai.yaml）")


def _state(config_path: str | None) -> AppState:
    return AppState(load_config(config_path))


def _unlock_interactive(state: AppState) -> None:
    from .crypto.keyring import WrongPassphrase

    passphrase = getpass.getpass("口令: ")
    try:
        state.unlock(passphrase)
    except WrongPassphrase:
        typer.secho("口令错误", fg=typer.colors.RED)
        raise typer.Exit(1)


@app.command()
def serve(config: str = CONFIG_OPT):
    """启动思脉服务（Locked 状态，浏览器打开 Web 管理端解锁）。"""
    import uvicorn

    from .api.app import create_app

    cfg = load_config(config)
    web_app = create_app(cfg)
    typer.echo(f"思脉服务启动：http://{cfg.web_bind}:{cfg.web_port}（处于锁定状态，请在网页中解锁）")
    # Access logs include full URLs.  Keep them disabled so a future query
    # endpoint cannot accidentally persist personal search text.
    uvicorn.run(
        web_app,
        host=cfg.web_bind,
        port=cfg.web_port,
        log_level="info",
        access_log=False,
    )


@app.command()
def init(config: str = CONFIG_OPT):
    """初始化加密保险库与数据库，并输出一次性离线恢复包。"""
    state = _state(config)
    if state.config.key_header_path.exists():
        typer.secho("保险库已存在，拒绝覆盖。", fg=typer.colors.RED)
        raise typer.Exit(1)
    pack_path = Path.cwd() / "simai-recovery-pack.json"
    if pack_path.exists():
        typer.secho(f"恢复包目标已存在，拒绝覆盖：{pack_path}", fg=typer.colors.RED)
        raise typer.Exit(1)
    p1 = getpass.getpass("设置口令（至少 8 位）: ")
    if len(p1) < 8:
        typer.secho("口令太短。", fg=typer.colors.RED)
        raise typer.Exit(1)
    if p1 != getpass.getpass("再次输入口令: "):
        typer.secho("两次输入不一致。", fg=typer.colors.RED)
        raise typer.Exit(1)
    recovery = state.initialize_vault(p1)
    try:
        fd = os.open(pack_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(recovery, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        typer.secho(
            "恢复包文件写入失败；保险库已经创建。请立即安全保存下面的一次性恢复包：",
            fg=typer.colors.RED,
        )
        typer.echo(json.dumps(recovery, ensure_ascii=False, indent=2))
        state.lock()
        raise typer.Exit(1)
    typer.secho(f"初始化完成：{state.config.db_path}", fg=typer.colors.GREEN)
    typer.secho(
        f"一次性离线恢复包已写入 {pack_path}\n请立即转移到安全的离线位置并从本机删除；服务器不保存该文件。",
        fg=typer.colors.YELLOW,
    )
    state.lock()


@app.command()
def capture(
    raw: bool = typer.Option(False, "--raw", help="不调用模型，按原文生成候选"),
    config: str = CONFIG_OPT,
):
    """从 stdin（或安全交互提示）读取思想，生成待确认候选。"""
    state = _state(config)
    _unlock_interactive(state)
    body = typer.prompt("内容") if sys.stdin.isatty() else sys.stdin.read()
    if not body.strip():
        raise typer.Exit(0)
    from .core import capture as capture_mod
    from .llm.client import ModelError, build_client

    with state.transaction() as tx:
        if raw:
            cards = [
                capture_mod.create_raw_candidate(
                    tx, state.keys.excerpt_key, body, source_binding_id="local_cli"
                )
            ]
        else:
            try:
                cards = capture_mod.run_capture(
                    tx,
                    build_client(state.config),
                    state.keys.excerpt_key,
                    body,
                    source_binding_id="local_cli",
                )
            except ModelError as exc:
                typer.secho(f"模型任务失败（明确失败，不写入）：{exc}", fg=typer.colors.RED)
                typer.echo("可使用 --raw 按原文生成候选。")
                raise typer.Exit(1)
    for card in cards:
        typer.echo(json.dumps(card, ensure_ascii=False, indent=2))
    typer.secho("候选已生成，请在 Web 待确认箱或 confirm 命令中确认。", fg=typer.colors.GREEN)
    state.lock()


@app.command()
def confirm(
    candidate_id: str = typer.Argument(..., help="候选 ID"),
    action: str = typer.Option(None, help="create_root/create_child/append/revise"),
    parent: str = typer.Option(None, help="父节点或目标节点 ID"),
    reject: bool = typer.Option(False, "--reject", help="拒绝该候选"),
    config: str = CONFIG_OPT,
):
    """确认或拒绝一个候选。"""
    state = _state(config)
    _unlock_interactive(state)
    from .core import candidates as cand_mod

    result = None
    with state.transaction() as tx:
        if reject:
            cand_mod.reject_candidate(tx, state.keys.audit_hmac_key, candidate_id)
            typer.echo("已拒绝。")
        else:
            result = cand_mod.confirm_candidate(
                tx,
                state.keys.audit_hmac_key,
                candidate_id,
                action=action,
                parent_id=parent,
                target_node_id=parent if action in ("append", "revise") else None,
            )
            typer.secho(
                f"已写入：{result['node_id']}（revision {result['revision_id']}）", fg=typer.colors.GREEN
            )
    if result and "node_id" in result:
        from .core import autorelations, search
        from .llm.client import build_client

        client = build_client(state.config)
        try:
            with state.transaction() as tx:
                search.upsert_embedding(tx, client, result["node_id"])
            rel_cfg = state.config.section("relations")
            if rel_cfg.get("auto_generate", True):
                with state.transaction() as tx:
                    autorelations.generate_for_node(
                        tx,
                        client,
                        state.keys.audit_hmac_key,
                        result["node_id"],
                        max_per_revision=int(rel_cfg.get("max_per_revision", 3)),
                        minimum_confidence=float(rel_cfg.get("minimum_confidence", 0.75)),
                    )
        except Exception:
            typer.secho("节点已确认；语义索引/自动关系暂不可用，可稍后重试。", fg=typer.colors.YELLOW)
    state.lock()


@daily_app.command("run")
def daily_run(config: str = CONFIG_OPT):
    """运行每日提取（供 OpenClaw cron 或手动调用）。锁定时不处理、不推进水位线。"""
    state = _state(config)
    from .core.daily import run_daily
    from .llm.client import build_client

    if not state.config.db_path.exists():
        typer.secho("数据库尚未初始化。", fg=typer.colors.RED)
        raise typer.Exit(1)
    # cron 场景下依附于常驻服务；独立运行时需交互解锁
    _unlock_interactive(state)
    summary = run_daily(state, build_client(state.config))
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
    state.lock()
    if summary.get("failed"):
        raise typer.Exit(1)


@daily_app.command("worker")
def daily_worker(
    binding: str = typer.Option(None, "--binding", help="用于授权调用的已启用来源 ID"),
    config: str = CONFIG_OPT,
):
    """供 command-cron 调用常驻 Web 服务；不接触口令或思想正文。"""
    import httpx

    from .api.auth import ensure_plugin_token

    cfg = load_config(config)
    enabled = [item for item in cfg.source_bindings() if item.enabled]
    chosen = (
        next((item for item in enabled if item.id == binding), None)
        if binding
        else next((item for item in enabled if item.passive_capture), enabled[0] if enabled else None)
    )
    if chosen is None:
        typer.secho("没有可用于每日 Worker 的已启用来源。", fg=typer.colors.RED)
        raise typer.Exit(1)
    token = ensure_plugin_token(cfg.plugin_token_path)
    url = f"http://{cfg.web_bind}:{cfg.web_port}/plugin-api/daily/run"
    try:
        with httpx.Client(timeout=180.0, trust_env=False) as client:
            response = client.post(
                url,
                json={"binding_id": chosen.id},
                headers={"X-Simai-Plugin-Token": token},
            )
    except httpx.HTTPError as exc:
        typer.secho(f"思脉服务不可达：{type(exc).__name__}", fg=typer.colors.RED)
        raise typer.Exit(1)
    if response.status_code != 200:
        typer.secho(f"每日 Worker 失败：HTTP {response.status_code}", fg=typer.colors.RED)
        raise typer.Exit(1)
    summary = response.json()
    # The endpoint contains counts/status only.  Never print message bodies.
    typer.echo(json.dumps(summary, ensure_ascii=False))
    if summary.get("failed"):
        raise typer.Exit(1)


@app.command()
def tree(root: str = typer.Option(None, help="局部根节点 ID"), config: str = CONFIG_OPT):
    """以缩进形式查看思维树。"""
    state = _state(config)
    _unlock_interactive(state)
    from .core import tree as tree_mod

    for node in tree_mod.subtree(state.conn, root):
        typer.echo(f"{'  ' * node['depth']}- {node['title']}  [{node['node_type']}] ({node['id']})")
    state.lock()


@app.command()
def query(config: str = CONFIG_OPT):
    """从 stdin（或安全交互提示）读取问题，并引用节点与版本回答。"""
    state = _state(config)
    _unlock_interactive(state)
    from .core.qa import answer_question
    from .llm.client import ModelError, build_client

    question = typer.prompt("问题") if sys.stdin.isatty() else sys.stdin.read()
    if not question.strip():
        raise typer.Exit(0)
    try:
        result = answer_question(state.conn, build_client(state.config), question)
    except ModelError as exc:
        typer.secho(f"问答模型不可用：{exc}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.echo(result["answer"])
    for c in result["citations"]:
        typer.secho(f"  引用: {c['path']} ({c['node_id']} rev {c['revision_no']})", fg=typer.colors.BLUE)
    for inf in result["new_inferences"]:
        typer.secho(f"  本次推论（非历史观点）: {inf}", fg=typer.colors.YELLOW)
    state.lock()


@app.command()
def export(
    format: str = typer.Option("markdown", help="simai-json/markdown/opml/graphml/json-canvas"),
    root: str = typer.Option(None, help="子树根节点 ID"),
    include_history: bool = typer.Option(False),
    encrypt: bool = typer.Option(False, "--encrypt", help="生成口令加密包（交互输入口令）"),
    config: str = CONFIG_OPT,
):
    """导出思维树（明文文件带 TTL，自动清理）。"""
    state = _state(config)
    _unlock_interactive(state)
    from .core import export as export_mod

    export_mod.cleanup_expired(
        state.config.export_dir,
        int(state.config.section("exports").get("plaintext_ttl_minutes", 30)),
    )
    export_passphrase = None
    if encrypt:
        export_passphrase = getpass.getpass("导出包口令（至少 8 位）: ")
        if len(export_passphrase) < 8:
            typer.secho("口令太短。", fg=typer.colors.RED)
            raise typer.Exit(1)
        if export_passphrase != getpass.getpass("再次输入导出包口令: "):
            typer.secho("两次输入不一致。", fg=typer.colors.RED)
            raise typer.Exit(1)
    result = export_mod.run_export(
        state.conn,
        state.config.export_dir,
        format,
        root_id=root,
        include_history=include_history,
        encryption_passphrase=export_passphrase,
    )
    typer.secho(
        f"已导出 {result['nodes']} 节点 / {result['relations']} 关系 → {result['path']}",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"SHA-256: {result['file_hash']}")
    state.lock()


@app.command()
def backup(config: str = CONFIG_OPT):
    """创建一致性加密备份并验证（含错误口令打开失败验证）。"""
    state = _state(config)
    _unlock_interactive(state)
    from .core.backup import create_backup

    result = create_backup(
        state.conn,
        state.keys.sqlcipher_hex(),
        state.config.backup_dir,
        state.config.key_header_path,
        state.config.inbox_dir,
    )
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    state.lock()


@app.command()
def restore(
    backup_path: str = typer.Argument(..., help="备份目录（backup-YYYY…）"),
    recovery_pack: str = typer.Option(None, "--recovery-pack", help="忘记口令时使用离线恢复包"),
    config: str = CONFIG_OPT,
):
    """校验并恢复一个备份集（拒绝覆盖现有数据库）。"""
    cfg = load_config(config)
    from .core.backup import BackupError, restore_backup
    from .crypto.keyring import (
        WrongPassphrase,
        unlock_vault,
        unlock_with_recovery_pack,
    )

    set_dir = Path(backup_path)
    header = next(set_dir.glob("*.header.json"), None)
    if header is None:
        typer.secho("备份集中缺少密钥头文件。", fg=typer.colors.RED)
        raise typer.Exit(1)
    if recovery_pack:
        try:
            pack = json.loads(Path(recovery_pack).read_text(encoding="utf-8"))
            keys = unlock_with_recovery_pack(header, pack)
        except Exception as exc:
            typer.secho(f"恢复包无效：{exc}", fg=typer.colors.RED)
            raise typer.Exit(1)
    else:
        passphrase = getpass.getpass("备份口令: ")
        try:
            keys = unlock_vault(header, passphrase)
        except WrongPassphrase:
            typer.secho("口令错误，无法打开备份。", fg=typer.colors.RED)
            raise typer.Exit(1)
    try:
        result = restore_backup(set_dir, cfg.data_dir, keys.sqlcipher_hex())
    except BackupError as exc:
        typer.secho(f"恢复失败：{exc}", fg=typer.colors.RED)
        raise typer.Exit(1)
    finally:
        keys.wipe()
    typer.secho(
        f"恢复完成并通过校验：{json.dumps(result['counts'], ensure_ascii=False)}", fg=typer.colors.GREEN
    )


@app.command("change-passphrase")
def change_passphrase(config: str = CONFIG_OPT):
    """修改口令（只重新封装 Vault Root Key，不重新加密数据库）。"""
    cfg = load_config(config)
    from .crypto.keyring import WrongPassphrase
    from .crypto.keyring import change_passphrase as do_change

    old = getpass.getpass("当前口令: ")
    new1 = getpass.getpass("新口令（至少 8 位）: ")
    if len(new1) < 8:
        typer.secho("口令太短。", fg=typer.colors.RED)
        raise typer.Exit(1)
    if new1 != getpass.getpass("再次输入新口令: "):
        typer.secho("两次输入不一致。", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        do_change(cfg.key_header_path, old, new1)
    except WrongPassphrase:
        typer.secho("当前口令错误。", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("口令已修改。正在运行的服务需重启后用新口令解锁。", fg=typer.colors.GREEN)


@app.command()
def doctor(config: str = CONFIG_OPT):
    """自检：SQLCipher/FTS5/外键/密码库/配置/网关连通性。"""
    from .db.engine import capability_report

    checks: list[tuple[str, bool, str]] = []
    notes: list[str] = []
    report = capability_report()
    checks.append(("sqlcipher3 驱动", report["driver"], ""))
    checks.append(
        ("SQLCipher cipher_version", bool(report["cipher_version"]), str(report["cipher_version"] or ""))
    )
    checks.append(("SQLite FTS5", report["fts5"], ""))
    checks.append(("外键约束", report["foreign_keys"], ""))
    checks.append(("SQLCipher 已激活", report["cipher_status"], ""))
    checks.append(("临时数据仅内存", report["memory_temp_store"], ""))
    try:
        import nacl  # noqa: F401

        checks.append(("PyNaCl (Argon2id/XChaCha20/SealedBox)", True, ""))
    except ImportError:
        checks.append(("PyNaCl (Argon2id/XChaCha20/SealedBox)", False, "pip install PyNaCl"))
    try:
        cfg = load_config(config)
        checks.append(("配置文件", True, str(cfg.path)))
        notes.append(
            f"保险库：{'已初始化' if cfg.key_header_path.is_file() else '尚未初始化（下一步运行 simai init）'}"
        )
        import httpx

        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                client.get(cfg.openclaw_gateway)
            notes.append(f"OpenClaw Gateway：可达（{cfg.openclaw_gateway}）")
        except Exception:
            notes.append(f"OpenClaw Gateway：暂不可达（不阻止初始化；{cfg.openclaw_gateway}）")
    except FileNotFoundError as exc:
        checks.append(("配置文件", False, str(exc)))

    failed = False
    for name, ok, note in checks:
        mark = "✓" if ok else "✗"
        color = typer.colors.GREEN if ok else typer.colors.RED
        typer.secho(f" {mark} {name}" + (f"  ({note})" if note else ""), fg=color)
        failed = failed or not ok
    for note in notes:
        typer.secho(f" ! {note}", fg=typer.colors.YELLOW)
    if failed:
        typer.secho("\n存在缺失能力：思脉将拒绝创建正式数据库（安全失败原则）。", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    typer.secho("\n环境就绪。", fg=typer.colors.GREEN)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
