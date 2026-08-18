"""End-to-end smoke test for the Simai core (no OpenClaw gateway needed).

Run:  python tests/smoke_test.py
Uses a temporary data directory; model tasks are stubbed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from simai.config import load_config
from simai.core import backup, candidates, capture, export, qa, relations, search, tree
from simai.core.daily import run_daily
from simai.core.state import AppState
from simai.crypto import keyring, sealed_inbox
from simai.db.engine import verify_capabilities
from simai.llm.client import ModelError
from simai.llm.schemas import (
    CaptureBatchResult,
    CaptureResult,
    ChildMergeProposal,
    ChildRelationProposal,
    DailyExtractItem,
    DailyExtractResult,
    DictationMergeResult,
    DictationTopic,
    PlacementResult,
    QueryAnswer,
    QueryCitation,
    QueryRelevance,
    ReorganizeResult,
)

PASS = "correct horse battery staple"

checks: list[str] = []


def ok(name: str, cond: bool = True) -> None:
    if not cond:
        print(f"FAIL {name}")
        sys.exit(1)
    checks.append(name)
    print(f"  ok {name}")


class FakeLLM:
    """Stub for daily extraction; echoes messages as candidates."""

    embedding_model = "stub-embedding"

    def __init__(self) -> None:
        self.answer_calls = 0
        self.judge_calls = 0

    def model_for(self, task: str) -> str:
        return f"openclaw/stub-{task}"

    def structured(self, task, system, user, schema):
        if schema is QueryRelevance:
            self.judge_calls += 1
            return QueryRelevance(node_ids=_relevant_stub_ids(user))
        if schema is CaptureBatchResult:
            parts = [part.strip() for part in user.split("；") if part.strip()]
            return CaptureBatchResult(
                items=[
                    CaptureResult(
                        candidate_type="opinion",
                        normalized_content=part,
                        title=part[:30],
                        proposed_action="create_root",
                        confidence=0.9,
                    )
                    for part in parts
                ]
            )
        if schema is PlacementResult:
            ids = re.findall(r"N-\d{8}-[0-9a-f]+", user)
            return PlacementResult(
                proposed_action="create_child" if ids else "create_root",
                proposed_parent_ids=ids[:1],
            )
        if schema is QueryAnswer:
            self.answer_calls += 1
            ids = re.findall(r"N-\d{8}-[0-9a-f]+", user)
            return QueryAnswer(
                answer="测试回答",
                citations=[QueryCitation(node_id=ids[0], revision_no=999, path="错误 / 路径")] if ids else [],
            )
        if schema is ReorganizeResult:
            ids = re.findall(r"NODE (N-\d{8}-[0-9a-f]+)", user)
            merges = []
            rels = []
            if len(ids) >= 2:
                merges.append(
                    ChildMergeProposal(
                        source_node_id=ids[0],
                        target_node_id=ids[1],
                        rationale="两个子节点表达的是同一个观点",
                        confidence=0.9,
                    )
                )
            if len(ids) >= 3:
                rels.append(
                    ChildRelationProposal(
                        from_node_id=ids[0],
                        to_node_id=ids[2],
                        relation_type="related_to",
                        rationale="主题相关",
                        confidence=0.8,
                    )
                )
            return ReorganizeResult(merges=merges, relations=rels)
        if schema is DictationMergeResult:
            # Deterministic mimic of the dictation-merge contract: numbered
            # owner items split into topics; otherwise ONE topic of the owner's
            # words plus any assistant point the owner endorsed ("对…").
            entries = []
            for line in user.splitlines():
                match = re.match(r"^\[\d+\]\[(主人|助手)\]\s*(.*)$", line)
                if match:
                    entries.append((match.group(1), match.group(2)))
            numbered = [b for s, b in entries if s == "主人" and re.match(r"^\d+[\.、．]", b)]
            if len(numbered) >= 2:
                return DictationMergeResult(
                    topics=[DictationTopic(title=b[:30], content=b) for b in numbered]
                )
            owner = [b for s, b in entries if s == "主人"]
            adopted = [
                f"（采纳自助手回复：{entries[i - 1][1]}）"
                for i, (s, b) in enumerate(entries)
                if s == "主人" and b.startswith("对") and i > 0 and entries[i - 1][0] == "助手"
            ]
            return DictationMergeResult(
                topics=[
                    DictationTopic(
                        title=(owner[0][:30] if owner else "速记"),
                        content="\n\n".join(owner + adopted),
                    )
                ]
            )
        if schema is DailyExtractResult:
            items = []
            for message_no, line in enumerate(user.splitlines(), start=1):
                if "] " in line:
                    body = line.split("] ", 1)[1]
                    if "观点" in body or "决定" in body:
                        items.append(
                            DailyExtractItem(
                                source_message_no=message_no,
                                source_excerpt=body,
                                capture=CaptureResult(
                                    candidate_type="opinion",
                                    normalized_content=body,
                                    title=body[:30],
                                    proposed_action="create_root",
                                    confidence=0.9,
                                ),
                            )
                        )
            return DailyExtractResult(items=items)
        raise AssertionError(f"unexpected schema {schema}")

    def embed(self, texts, *, kind="document"):
        assert kind in ("document", "query")
        return [[1.0, 0.0] for _ in texts]


def _relevant_stub_ids(user: str) -> list[str]:
    """Keep candidate nodes whose text overlaps the question's CJK bigrams."""
    qline = user.split("QUESTION:", 1)[1].split("\n", 1)[0] if "QUESTION:" in user else user
    chars = re.findall(r"[\u4e00-\u9fff]", qline)
    needles = {"".join(chars[i : i + 2]) for i in range(max(0, len(chars) - 1))}
    if len(chars) == 1:
        needles.add(chars[0])
    needles.update(re.findall(r"[A-Za-z0-9_]{2,}", qline))
    kept: list[str] = []
    for block in re.split(r"\n(?=NODE )", user):
        match = re.match(r"NODE (N-\d{8}-[0-9a-f]+)", block.strip())
        if match and needles and any(needle in block for needle in needles):
            kept.append(match.group(1))
    return kept


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="simai-test-"))
    try:
        run(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"\nAll {len(checks)} checks passed.")


def run(workdir: Path) -> None:
    data_dir = workdir / "data"
    cfg_path = workdir / "simai.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {"profile": "local_wsl", "data_dir": str(data_dir)},
                "profiles": {"local_wsl": {"openclaw_gateway": "http://127.0.0.1:1"}},
                "source_bindings": [
                    {
                        "id": "local_cli",
                        "channel": "cli",
                        "account_id": "local",
                        "sender_key": "owner",
                        "enabled": True,
                        "passive_capture": True,
                    }
                ],
                "daily_capture": {"cutoff_delay_minutes": 0, "max_messages_per_run": 1},
            }
        ),
        encoding="utf-8",
    )

    print("capabilities")
    verify_capabilities()
    ok("sqlcipher + fts5 + foreign keys present")

    print("vault")
    config = load_config(cfg_path)
    state = AppState(config)
    recovery = state.initialize_vault(PASS)
    ok("vault initialized", config.key_header_path.is_file() and config.db_path.is_file())
    ok("recovery pack returned", "vault_root_key" in recovery)
    ok("vault header is owner-only", os.stat(config.key_header_path).st_mode & 0o777 == 0o600)

    # database file must be unreadable as plain sqlite
    import sqlite3

    plain = sqlite3.connect(config.db_path)
    try:
        plain.execute("SELECT count(*) FROM sqlite_master").fetchone()
        raise AssertionError("plain sqlite could read the encrypted db!")
    except sqlite3.DatabaseError:
        ok("database unreadable without key")
    finally:
        plain.close()

    state.lock()
    try:
        keyring.unlock_vault(config.key_header_path, "wrong password")
        raise AssertionError("wrong passphrase accepted")
    except keyring.WrongPassphrase:
        ok("wrong passphrase rejected")
    state.unlock(PASS)
    ok("unlock with correct passphrase")
    keys = state.keys

    print("tree")
    with state.transaction() as tx:
        root = tree.create_node(tx, keys.audit_hmac_key, "组织管理", "关于组织管理的思考", "topic")
        child = tree.create_node(
            tx, keys.audit_hmac_key, "小团队自治", "小团队应当自治", "opinion", parent_id=root["node_id"]
        )
        grand = tree.create_node(
            tx, keys.audit_hmac_key, "自治的边界", "自治需要边界", "idea", parent_id=child["node_id"]
        )
    ok("nodes created", len(tree.subtree(state.conn)) == 3)
    with state.transaction() as tx:
        for node_id in (root["node_id"], child["node_id"], grand["node_id"]):
            search.upsert_embedding(tx, FakeLLM(), node_id)
    path = tree.node_path(state.conn, grand["node_id"])
    ok("path root→node", [p["title"] for p in path] == ["组织管理", "小团队自治", "自治的边界"])

    try:
        with state.transaction() as tx:
            tree.move_node(tx, keys.audit_hmac_key, root["node_id"], grand["node_id"])
        raise AssertionError("cycle move accepted")
    except tree.TreeError:
        ok("cycle move refused")

    root_rev = state.conn.execute(
        "SELECT current_revision_id FROM nodes WHERE id = ?", (root["node_id"],)
    ).fetchone()[0]
    try:
        state.conn.execute(
            "UPDATE nodes SET current_revision_id = ? WHERE id = ?",
            (root_rev, child["node_id"]),
        )
        raise AssertionError("cross-node current_revision_id accepted")
    except Exception:
        ok("current_revision_id must belong to same node")

    with state.transaction() as tx:
        rel_id = relations.add_relation(
            tx,
            keys.audit_hmac_key,
            grand["node_id"],
            child["node_id"],
            "supports",
            origin="ai",
            rationale="边界支撑自治",
            confidence=0.9,
        )
    ok("ai relation created")
    try:
        with state.transaction() as tx:
            relations.add_relation(
                tx,
                keys.audit_hmac_key,
                root["node_id"],
                child["node_id"],
                "supersedes",
                origin="ai",
                confidence=0.9,
            )
        raise AssertionError("ai supersedes accepted")
    except relations.RelationError:
        ok("ai supersedes refused")

    with state.transaction() as tx:
        tree.update_node(
            tx, keys.audit_hmac_key, child["node_id"], "revise", body="小团队应当自治，但要有边界"
        )
    row = state.conn.execute("SELECT state FROM relations WHERE id = ?", (rel_id,)).fetchone()
    ok("relation stale after revise", row["state"] == "stale")
    with state.transaction() as tx:
        reconfirmed_id = relations.set_relation_state(tx, keys.audit_hmac_key, rel_id, "confirmed")
    reconfirmed = state.conn.execute("SELECT * FROM relations WHERE id = ?", (reconfirmed_id,)).fetchone()
    current_child_rev = tree.get_current_revision(state.conn, child["node_id"])
    ok("stale relation reconfirm creates successor", reconfirmed_id != rel_id)
    ok(
        "reconfirmed relation binds current revision",
        reconfirmed["to_revision_id"] == current_child_rev["id"]
        and reconfirmed["supersedes_relation_id"] == rel_id,
    )
    ok("revision history kept", len(tree.revision_timeline(state.conn, child["node_id"])) == 2)
    with state.transaction() as tx:
        tree.move_node(tx, keys.audit_hmac_key, grand["node_id"], root["node_id"])
        tree.update_node(tx, keys.audit_hmac_key, grand["node_id"], "revise", node_type="method")
        tree.restore_revision(tx, keys.audit_hmac_key, grand["node_id"], 1)
    restored_grand = tree.get_node(state.conn, grand["node_id"])
    ok(
        "revision restore includes parent and node type",
        restored_grand["parent_id"] == child["node_id"] and restored_grand["node_type"] == "idea",
    )
    try:
        with state.transaction() as tx:
            tree.update_node(tx, keys.audit_hmac_key, grand["node_id"], "revise", node_type="INVALID")
        raise AssertionError("invalid node type accepted")
    except tree.TreeError:
        ok("invalid updated node type refused")

    with state.transaction() as tx:
        moving_rel = relations.add_relation(
            tx, keys.audit_hmac_key, grand["node_id"], root["node_id"], "related_to", origin="user"
        )
        tree.move_node(tx, keys.audit_hmac_key, grand["node_id"], root["node_id"])
    ok(
        "relation stale after move revision",
        state.conn.execute("SELECT state FROM relations WHERE id = ?", (moving_rel,)).fetchone()[0]
        == "stale",
    )

    print("candidates")
    with state.transaction() as tx:
        card = capture.create_raw_candidate(tx, keys.excerpt_key, "记录：产品应该聚焦单用户体验")
    cand_id = card["candidate_id"]
    ok("raw candidate pending", card["status"] == "pending" and card["source_excerpt"])
    with state.transaction() as tx:
        result = candidates.confirm_candidate(
            tx, keys.audit_hmac_key, cand_id, action="create_child", parent_id=root["node_id"]
        )
    blob = state.conn.execute(
        "SELECT source_excerpt_ciphertext FROM candidates WHERE id = ?", (cand_id,)
    ).fetchone()[0]
    ok("excerpt ciphertext wiped after confirm", blob is None)
    ok("confirmed node in tree", tree.get_node(state.conn, result["node_id"])["parent_id"] == root["node_id"])
    with state.transaction() as tx:
        revised = capture.create_raw_candidate(tx, keys.excerpt_key, "新的正文")
        revised_id = revised["candidate_id"]
    with state.transaction() as tx:
        candidates.confirm_candidate(
            tx,
            keys.audit_hmac_key,
            revised_id,
            action="revise",
            target_node_id=result["node_id"],
            edited_title="用户修改后的标题",
            node_type="insight",
        )
    ok(
        "revise applies edited title and node type",
        tree.get_node(state.conn, result["node_id"])["title"] == "用户修改后的标题"
        and tree.get_node(state.conn, result["node_id"])["node_type"] == "insight",
    )

    with state.transaction() as tx:
        archive_parent = tree.create_node(tx, keys.audit_hmac_key, "待归档分支", "", "topic")
        archive_child = tree.create_node(
            tx, keys.audit_hmac_key, "待归档子项", "", parent_id=archive_parent["node_id"]
        )
        tree.archive_node(tx, keys.audit_hmac_key, archive_parent["node_id"])
    archived_states = state.conn.execute(
        "SELECT state FROM nodes WHERE id IN (?,?)",
        (archive_parent["node_id"], archive_child["node_id"]),
    ).fetchall()
    ok("archiving a branch archives descendants", {row[0] for row in archived_states} == {"archived"})

    try:
        with state.transaction() as tx:
            tree.create_node(
                tx,
                keys.audit_hmac_key,
                "不应创建的子项",
                "",
                parent_id=archive_parent["node_id"],
            )
        raise AssertionError("child created below an archived parent")
    except tree.TreeError:
        ok("create refuses an inactive parent")

    for label, operation in (
        (
            "update refuses an inactive target",
            lambda tx: tree.update_node(
                tx, keys.audit_hmac_key, archive_parent["node_id"], "revise", body="不可修改"
            ),
        ),
        (
            "move refuses an inactive target",
            lambda tx: tree.move_node(tx, keys.audit_hmac_key, archive_parent["node_id"], None),
        ),
        (
            "move refuses an inactive parent",
            lambda tx: tree.move_node(tx, keys.audit_hmac_key, grand["node_id"], archive_parent["node_id"]),
        ),
        (
            "merge refuses an inactive source",
            lambda tx: tree.merge_nodes(tx, keys.audit_hmac_key, archive_parent["node_id"], root["node_id"]),
        ),
        (
            "merge refuses an inactive target",
            lambda tx: tree.merge_nodes(tx, keys.audit_hmac_key, root["node_id"], archive_parent["node_id"]),
        ),
        (
            "archive refuses an inactive target",
            lambda tx: tree.archive_node(tx, keys.audit_hmac_key, archive_parent["node_id"]),
        ),
        (
            "revision restore refuses an inactive target",
            lambda tx: tree.restore_revision(tx, keys.audit_hmac_key, archive_parent["node_id"], 1),
        ),
    ):
        try:
            with state.transaction() as tx:
                operation(tx)
            raise AssertionError(label)
        except tree.TreeError:
            ok(label)

    try:
        with state.transaction() as tx:
            relations.add_relation(
                tx,
                keys.audit_hmac_key,
                archive_parent["node_id"],
                root["node_id"],
                "related_to",
                "user",
            )
        raise AssertionError("relation created with an inactive endpoint")
    except relations.RelationError:
        ok("relations refuse inactive endpoints")

    with state.transaction() as tx:
        restore_parent = tree.create_node(tx, keys.audit_hmac_key, "历史父节点", "", "topic")
        restore_child = tree.create_node(
            tx,
            keys.audit_hmac_key,
            "待恢复节点",
            "第一版",
            parent_id=restore_parent["node_id"],
        )
        tree.move_node(tx, keys.audit_hmac_key, restore_child["node_id"], None)
        tree.archive_node(tx, keys.audit_hmac_key, restore_parent["node_id"])
    try:
        with state.transaction() as tx:
            tree.restore_revision(tx, keys.audit_hmac_key, restore_child["node_id"], 1)
        raise AssertionError("revision restored below an archived historical parent")
    except tree.TreeError:
        ok("restore refuses an inactive historical parent")

    with state.transaction() as tx:
        merge_target = tree.create_node(tx, keys.audit_hmac_key, "合并目标", "", "topic")
        merge_source = tree.create_node(tx, keys.audit_hmac_key, "合并来源", "来源正文", "idea")
        merge_child = tree.create_node(
            tx,
            keys.audit_hmac_key,
            "合并来源的子项",
            "",
            parent_id=merge_source["node_id"],
        )
        tree.merge_nodes(tx, keys.audit_hmac_key, merge_source["node_id"], merge_target["node_id"])
    merged_source_row = tree.get_node(state.conn, merge_source["node_id"])
    merged_child_row = tree.get_node(state.conn, merge_child["node_id"])
    ok(
        "merge reparents active children before deactivating source",
        merged_source_row["state"] == "merged"
        and merged_child_row["state"] == "active"
        and merged_child_row["parent_id"] == merge_target["node_id"],
    )
    dangling_active = state.conn.execute(
        """SELECT c.id FROM nodes c JOIN nodes p ON p.id = c.parent_id
           WHERE c.state = 'active' AND p.state <> 'active'"""
    ).fetchall()
    ok("no active node has an inactive parent", not dangling_active)

    print("multi-thought capture")
    multi_hmac = "multi-message-hmac"
    with state.transaction() as tx:
        multi_cards = capture.run_capture(
            tx,
            FakeLLM(),
            keys.excerpt_key,
            "观点一；观点二",
            source_binding_id="local_cli",
            message_hmac=multi_hmac,
        )
    ok("one message split into two candidates", len(multi_cards) == 2)
    before_repeat = state.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE message_hmac = ?", (multi_hmac,)
    ).fetchone()[0]
    with state.transaction() as tx:
        repeated_cards = capture.run_capture(
            tx,
            FakeLLM(),
            keys.excerpt_key,
            "观点一；观点二",
            source_binding_id="local_cli",
            message_hmac=multi_hmac,
        )
    after_repeat = state.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE message_hmac = ?", (multi_hmac,)
    ).fetchone()[0]
    ok(
        "message-level capture remains idempotent",
        len(repeated_cards) == 2 and before_repeat == after_repeat == 2,
    )

    print("search")
    from simai.core.search import keyword_search, reindex_all, semantic_search
    from simai.core.textseg import build_match_query
    from simai.llm.client import _embedding_route, _prefixed_inputs

    ok(
        "embedding route uses the simai agent and a slash-free override",
        _embedding_route("embeddinggemma-300m") == ("openclaw/simai", "embeddinggemma-300m")
        and _embedding_route("openclaw/simai") == ("openclaw/simai", None)
        and _embedding_route("openai/Qwen/Qwen3-Embedding-8B")
        == ("openclaw/simai", "openai/Qwen/Qwen3-Embedding-8B"),
    )
    ok(
        "embeddinggemma gets asymmetric prompt prefixes, other models do not",
        _prefixed_inputs(["文本"], "embeddinggemma-300m", "query")
        == ["task: search result | query: 文本"]
        and _prefixed_inputs(["文本"], "embeddinggemma-300m", "document")
        == ["title: none | text: 文本"]
        and _prefixed_inputs(["文本"], "openai/Qwen/Qwen3-Embedding-8B", "query") == ["文本"],
    )

    hits = keyword_search(state.conn, "自治")
    ok("fts keyword search", any(h["node_id"] == child["node_id"] for h in hits))
    sentence_hits = keyword_search(state.conn, "我对小团队自治有什么看法？")
    ok(
        "fts keyword search matches a natural-language question",
        any(h["node_id"] == child["node_id"] for h in sentence_hits),
    )
    ok(
        "full-width punctuation never reaches the MATCH expression",
        "？" not in build_match_query("自治？") and build_match_query("？，。") == "",
    )
    reindex = reindex_all(state.conn, FakeLLM())
    ok("reindex writes an embedding for every active node", reindex["written"] == reindex["nodes"] > 0)
    semantic_hits = semantic_search(state.conn, FakeLLM(), "完全不同的措辞", limit=3)
    ok("semantic search ranks stored vectors even without keyword overlap", bool(semantic_hits))
    stub = FakeLLM()
    recalled = search.recall_for_qa(state.conn, stub, "自治")
    ok(
        "small trees recall every active node before judging",
        len(recalled) == state.conn.execute("SELECT COUNT(*) FROM nodes WHERE state='active'").fetchone()[0],
    )
    answer = qa.answer_question(state.conn, stub, "自治")
    citation = answer["citations"][0]
    expected_rev = tree.get_current_revision(state.conn, citation["node_id"])
    ok(
        "query citation path/revision canonicalized from database",
        citation["revision_no"] == expected_rev["revision_no"] and citation["path"] != "错误 / 路径",
    )
    ok("query judge runs before the answer call", stub.judge_calls == 1 and stub.answer_calls == 1)
    ok(
        "query judge drops nodes that do not answer the question",
        all(
            "自治" in (rev["title"] + rev["body"])
            for c in answer["citations"]
            for rev in [tree.get_current_revision(state.conn, c["node_id"])]
            if rev is not None
        )
        and answer["citations"],
    )
    empty_stub = FakeLLM()
    empty = qa.answer_question(state.conn, empty_stub, "番茄炒蛋怎么做")
    ok(
        "empty judge does not fall back to the recall set",
        empty["answer"].startswith("思维树中没有找到")
        and empty["citations"] == []
        and empty_stub.judge_calls == 1
        and empty_stub.answer_calls == 0,
    )

    print("sealed inbox + daily")
    pub = keyring.inbox_public_key(config.key_header_path)

    def seal(body: str, message_id: str, capture_mode: str = "passive"):
        return sealed_inbox.seal_item(
            config.inbox_dir,
            pub,
            "local_cli",
            body,
            channel="cli",
            account_id="local",
            sender_key="owner",
            conversation_id=None,
            is_group=False,
            message_id=message_id,
            capture_mode=capture_mode,
        )

    seal("我的观点是备份必须常态演练", "m1")
    seal("今天天气怎么样", "m2")
    seal("我的观点是备份必须常态演练", "m1")  # dup
    summary = run_daily(state, FakeLLM())
    summary_next = run_daily(state, FakeLLM())
    ok(
        "daily backlog is processed in bounded batches",
        summary["processed"] == 1 and summary["backlog_remaining"] == 1 and summary_next["processed"] == 1,
    )
    ok("daily extracted opinion only", summary["candidates"] + summary_next["candidates"] == 1)
    daily_candidate = state.conn.execute(
        """SELECT proposed_action, proposed_parent_ids FROM candidates
           WHERE normalized_content LIKE '%备份必须常态演练%'""",
    ).fetchone()
    ok(
        "daily candidate receives tree placement",
        daily_candidate["proposed_action"] == "create_child"
        and json.loads(daily_candidate["proposed_parent_ids"]),
    )
    ok("inbox emptied after commit", len(sealed_inbox.list_items(config.inbox_dir)) == 0)
    summary2 = run_daily(state, FakeLLM())
    ok("daily idempotent", summary2["candidates"] == 0)
    state.daily_lock.acquire()
    try:
        concurrent = run_daily(state, FakeLLM())
    finally:
        state.daily_lock.release()
    ok("concurrent daily run is refused", concurrent.get("already_running") is True)
    mismatched = sealed_inbox.seal_item(
        config.inbox_dir,
        pub,
        "local_cli",
        "不应导入的错误身份",
        channel="cli",
        account_id="local",
        sender_key="someone-else",
        conversation_id=None,
        is_group=False,
        message_id="bad-identity",
    )
    mismatch_summary = run_daily(state, FakeLLM())
    ok(
        "daily revalidates the encrypted identity tuple",
        mismatch_summary["refused_binding"] == 1 and mismatched.is_file(),
    )
    sealed_inbox.delete_item(mismatched)
    seal("禁用时必须保留", "disabled")
    config.raw["daily_capture"]["enabled"] = False
    disabled_summary = run_daily(state, FakeLLM())
    ok(
        "daily enabled switch prevents processing",
        disabled_summary["disabled"] and len(sealed_inbox.list_items(config.inbox_dir)) == 1,
    )
    config.raw["daily_capture"]["enabled"] = True
    run_daily(state, FakeLLM())

    with state.transaction() as tx:
        snoozed = capture.create_raw_candidate(tx, keys.excerpt_key, "暂缓的想法")
    with state.transaction() as tx:
        candidates.snooze_candidate(tx, keys.audit_hmac_key, snoozed["candidate_id"])
    summary3 = run_daily(state, FakeLLM())
    ok("daily wakes snoozed candidates", summary3["woken_snoozed"] == 1 and summary3["notify"])
    row = state.conn.execute(
        "SELECT status FROM candidates WHERE id = ?", (snoozed["candidate_id"],)
    ).fetchone()
    ok("snoozed back to pending", row["status"] == "pending")

    print("explicit capture skips daily")
    import hashlib
    import hmac as hmac_mod

    seal("我的观点是主动记录过的内容不应再被每日提取", "m-explicit")
    explicit_hmac = hmac_mod.new(
        keys.audit_hmac_key,
        b"local_cli|mid:m-explicit",
        hashlib.sha256,
    ).hexdigest()
    pending_before = state.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
    ).fetchone()[0]
    with state.transaction() as tx:
        capture.create_raw_candidate(
            tx,
            keys.excerpt_key,
            "我的观点是主动记录过的内容不应再被每日提取",
            source_binding_id="local_cli",
            message_hmac=explicit_hmac,
        )
    summary_ex = run_daily(state, FakeLLM())
    pending_after = state.conn.execute("SELECT COUNT(*) FROM candidates WHERE status = 'pending'").fetchone()[
        0
    ]
    ok(
        "daily skipped explicitly handled message",
        summary_ex["skipped_already_handled"] >= 1 and summary_ex["candidates"] == 0,
    )
    ok("explicit capture did not yield a second daily candidate", pending_after == pending_before + 1)
    ok("explicit inbox item removed after skip", len(sealed_inbox.list_items(config.inbox_dir)) == 0)

    explicit_before = state.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
    ).fetchone()[0]
    seal("请保存这个，但它不含每日筛选关键词", "m-locked-explicit", "explicit")
    explicit_summary = run_daily(state, FakeLLM())
    explicit_after = state.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
    ).fetchone()[0]
    ok(
        "sealed explicit capture always becomes a candidate",
        explicit_summary["candidates"] == 1 and explicit_after == explicit_before + 1,
    )
    long_explicit_before = explicit_after
    seal("长" * 61000, "m-long-explicit", "explicit")
    long_explicit_summary = run_daily(state, FakeLLM())
    long_explicit_after = state.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
    ).fetchone()[0]
    ok(
        "explicit capture bypasses the passive model prompt-size limit",
        long_explicit_summary["candidates"] == 1 and long_explicit_after == long_explicit_before + 1,
    )
    duplicate_before = long_explicit_after
    seal("同一个消息同时经过被动观察和主动工具", "m-dual-mode", "passive")
    seal("同一个消息同时经过被动观察和主动工具", "m-dual-mode", "explicit")
    duplicate_summary = run_daily(state, FakeLLM())
    duplicate_after = state.conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE status = 'pending'"
    ).fetchone()[0]
    ok(
        "explicit intent wins over duplicate passive capture",
        duplicate_summary["candidates"] == 1 and duplicate_after == duplicate_before + 1,
    )

    race_text = "我的观点是并发显式记录只能生成一个候选"
    race_message_id = "m-race-explicit"
    race_hmac = hmac_mod.new(
        keys.audit_hmac_key,
        f"local_cli|mid:{race_message_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    seal(race_text, race_message_id, "passive")
    race_before = state.conn.execute("SELECT COUNT(*) FROM candidates WHERE status = 'pending'").fetchone()[0]

    class ExplicitDuringDaily(FakeLLM):
        injected = False

        def structured(self, task, system, user, schema):
            if task == "daily_extract" and not self.injected:
                self.injected = True
                with state.transaction() as tx:
                    capture.create_raw_candidate(
                        tx,
                        keys.excerpt_key,
                        race_text,
                        source_binding_id="local_cli",
                        message_hmac=race_hmac,
                    )
            return super().structured(task, system, user, schema)

    race_summary = run_daily(state, ExplicitDuringDaily())
    race_after = state.conn.execute("SELECT COUNT(*) FROM candidates WHERE status = 'pending'").fetchone()[0]
    ok(
        "daily rechecks receipts after model calls to avoid an explicit race",
        race_summary["candidates"] == 0
        and race_summary["skipped_already_handled"] >= 1
        and race_after == race_before + 1,
    )

    print("dictation session merge")
    config.raw["daily_capture"]["max_messages_per_run"] = 10

    def seal_dictation(
        body: str, message_id: str, dictation_id: str | None, speaker: str = "owner"
    ):
        return sealed_inbox.seal_item(
            config.inbox_dir,
            pub,
            "local_cli",
            body,
            channel="cli",
            account_id="local",
            sender_key="owner",
            conversation_id=None,
            is_group=False,
            message_id=message_id,
            capture_mode="explicit",
            dictation_id=dictation_id,
            speaker=speaker,
        )

    seal_dictation("产品加密应默认开启", "dict-1", "session-a")
    seal_dictation("而且密钥要支持定期轮换", "dict-2", "session-a")
    seal_dictation("独立的一条显式记录", "dict-3", None)
    dictation_summary = run_daily(state, FakeLLM())
    ok(
        "dictation session merges into one candidate plus one raw",
        dictation_summary["processed"] == 3 and dictation_summary["candidates"] == 2,
    )
    merged_row = state.conn.execute(
        """SELECT normalized_content, title, proposed_action FROM candidates
           WHERE normalized_content LIKE '%产品加密应默认开启%' AND status = 'pending'"""
    ).fetchone()
    ok(
        "merged session keeps both utterances in spoken order",
        merged_row is not None
        and "产品加密应默认开启\n\n而且密钥要支持定期轮换" == merged_row["normalized_content"]
        and merged_row["title"] == "产品加密应默认开启",
    )
    single_row = state.conn.execute(
        "SELECT id FROM candidates WHERE normalized_content = '独立的一条显式记录' AND status = 'pending'"
    ).fetchone()
    ok("explicit item outside a session stays a separate candidate", single_row is not None)

    # Endorsed assistant content is woven into the topic; explicit numbering of
    # unrelated items splits the session into separate topics.
    seal_dictation("语音接口应该做成流式", "dict-4", "session-b")
    seal_dictation("建议同时支持增量转写和断点续传", "dict-5", "session-b", speaker="assistant")
    seal_dictation("对，就按这个方案来", "dict-6", "session-b")
    seal_dictation("1. 加密默认开启的推进计划", "dict-7", "session-c")
    seal_dictation("2. 周末给家里换个路由器", "dict-8", "session-c")
    rich_summary = run_daily(state, FakeLLM())
    ok(
        "dictation model composes one merged topic plus two numbered topics",
        rich_summary["processed"] == 5 and rich_summary["candidates"] == 3,
    )
    endorsed_row = state.conn.execute(
        """SELECT normalized_content FROM candidates
           WHERE normalized_content LIKE '%语音接口应该做成流式%' AND status = 'pending'"""
    ).fetchone()
    ok(
        "owner-endorsed assistant point is woven into the merged topic",
        endorsed_row is not None
        and "对，就按这个方案来" in endorsed_row["normalized_content"]
        and "采纳自助手回复：建议同时支持增量转写和断点续传" in endorsed_row["normalized_content"],
    )
    numbered_count = state.conn.execute(
        """SELECT COUNT(*) FROM candidates WHERE status = 'pending'
           AND (normalized_content LIKE '1.%' OR normalized_content LIKE '2.%')"""
    ).fetchone()[0]
    ok("explicitly numbered unrelated items become separate topics", numbered_count == 2)

    # A dictation-merge model outage must never lose or block explicit capture:
    # the owner's verbatim words fall back to one topic; assistant context is
    # dropped because nobody can decide what was endorsed.
    class DictationModelDown(FakeLLM):
        def structured(self, task, system, user, schema):
            if schema is DictationMergeResult:
                raise ModelError("dictation model down")
            return super().structured(task, system, user, schema)

    seal_dictation("模型故障时也不能丢的想法", "dict-9", "session-d")
    seal_dictation("这是助手的插话", "dict-10", "session-d", speaker="assistant")
    down_summary = run_daily(state, DictationModelDown())
    fallback_row = state.conn.execute(
        """SELECT normalized_content FROM candidates
           WHERE normalized_content LIKE '%模型故障时也不能丢的想法%' AND status = 'pending'"""
    ).fetchone()
    ok(
        "dictation model outage falls back to verbatim owner-only merge",
        down_summary["candidates"] == 1
        and fallback_row is not None
        and "助手的插话" not in fallback_row["normalized_content"],
    )

    # 结束记录 closes the session explicitly: closed sessions bypass the
    # cutoff quiet window entirely; unclosed young sessions wait as a whole.
    from simai.core import dictation as dictation_mod

    config.raw["daily_capture"]["cutoff_delay_minutes"] = 30
    seal_dictation("刚说完就应该被整理的想法", "dict-11", "session-e")
    seal_dictation("这一句也属于同一次速记", "dict-12", "session-e")
    waiting = run_daily(state, FakeLLM())
    ok(
        "unclosed young session waits out the quiet window",
        waiting["processed"] == 0 and waiting["candidates"] == 0,
    )
    dictation_mod.mark_closed(config.inbox_dir, "local_cli", "session-e")
    closed_run = run_daily(state, FakeLLM())
    closed_row = state.conn.execute(
        """SELECT normalized_content FROM candidates
           WHERE normalized_content LIKE '%刚说完就应该被整理的想法%' AND status = 'pending'"""
    ).fetchone()
    ok(
        "closed session bypasses the cutoff and merges immediately",
        closed_run["processed"] == 2
        and closed_run["candidates"] == 1
        and closed_row is not None
        and "这一句也属于同一次速记" in closed_row["normalized_content"],
    )
    ok(
        "fully processed session is forgotten by the closure registry",
        ("local_cli", "session-e") not in dictation_mod.closed_keys(config.inbox_dir),
    )
    config.raw["daily_capture"]["cutoff_delay_minutes"] = 0
    config.raw["daily_capture"]["max_messages_per_run"] = 1

    print("legacy envelope without dictation_id still opens")
    from datetime import UTC, datetime

    from nacl.public import SealedBox

    legacy_envelope = {
        "schema_version": 2,
        "binding_id": "local_cli",
        "message_id": "legacy-1",
        "session_key": None,
        "captured_at": datetime.now(UTC).isoformat(timespec="microseconds"),
        "body": "旧插件写入的密文",
        "capture_mode": "passive",
        "channel": "cli",
        "account_id": "local",
        "sender_key": "owner",
        "conversation_id": None,
        "is_group": False,
    }
    legacy_path = config.inbox_dir / "00000000000000000001-legacy.sealed"
    legacy_path.write_bytes(
        SealedBox(pub).encrypt(json.dumps(legacy_envelope, ensure_ascii=False).encode("utf-8"))
    )
    legacy_path.chmod(0o600)
    legacy_item = sealed_inbox.open_item(legacy_path, keys.inbox_private_key)
    ok(
        "pre-dictation envelopes decode with dictation_id=None and speaker=owner",
        legacy_item.dictation_id is None
        and legacy_item.speaker == "owner"
        and legacy_item.body == "旧插件写入的密文",
    )
    sealed_inbox.delete_item(legacy_path)

    print("reorganize children")
    from simai.core import reorganize as reorganize_mod

    with state.transaction() as tx:
        reorg_parent = tree.create_node(tx, keys.audit_hmac_key, "整理演练主题", "", "topic", None)
        tree.create_node(
            tx, keys.audit_hmac_key, "加密默认开启", "产品加密应当默认开启", "opinion",
            reorg_parent["node_id"],
        )
        tree.create_node(
            tx, keys.audit_hmac_key, "默认加密立场", "加密必须是产品的默认设置", "opinion",
            reorg_parent["node_id"],
        )
        tree.create_node(
            tx, keys.audit_hmac_key, "密钥轮换", "密钥应当支持定期轮换", "idea",
            reorg_parent["node_id"],
        )
    with state.transaction() as tx:
        reorg_summary = reorganize_mod.reorganize_children(
            tx, FakeLLM(), keys.audit_hmac_key, keys.excerpt_key, reorg_parent["node_id"]
        )
    ok(
        "reorganize records proposals only",
        reorg_summary["children"] == 3
        and reorg_summary["merge_candidates"] == 1
        and reorg_summary["relation_proposals"] == 1,
    )
    merge_cand = state.conn.execute(
        "SELECT * FROM candidates WHERE proposed_action = 'merge' AND status = 'pending'"
    ).fetchone()
    merge_endpoints = json.loads(merge_cand["proposed_parent_ids"])
    ok(
        "merge proposal does not touch the tree before confirmation",
        len(merge_endpoints) == 2
        and all(
            state.conn.execute("SELECT state FROM nodes WHERE id = ?", (nid,)).fetchone()["state"]
            == "active"
            for nid in merge_endpoints
        ),
    )
    reorg_rel = state.conn.execute(
        "SELECT state, origin FROM relations WHERE model_profile = 'reorganize'"
    ).fetchone()
    ok(
        "reorganize relation lands as ai_generated for review",
        reorg_rel["state"] == "ai_generated" and reorg_rel["origin"] == "ai",
    )
    ok(
        "pending AI relations are listable for the inbox",
        any(r["model_profile"] == "reorganize" for r in relations.pending_ai(state.conn)),
    )
    with state.transaction() as tx:
        candidates.confirm_candidate(
            tx,
            keys.audit_hmac_key,
            merge_cand["id"],
            action="merge",
            target_node_id=merge_endpoints[0],
            parent_id=merge_endpoints[1],
        )
    merged_source_state = state.conn.execute(
        "SELECT state FROM nodes WHERE id = ?", (merge_endpoints[0],)
    ).fetchone()["state"]
    merged_target_body = tree.get_current_revision(state.conn, merge_endpoints[1])["body"]
    ok(
        "confirmed merge appends source into target and retires the source",
        merged_source_state == "merged" and "产品加密应当默认开启" in merged_target_body,
    )

    print("deep reorganize")
    # Exhaust the deep scan (per-run budget may defer scopes on a big tree).
    for _ in range(5):
        with state.transaction() as tx:
            deep1 = reorganize_mod.reorganize_tree(
                tx, FakeLLM(), keys.audit_hmac_key, keys.excerpt_key
            )
        if deep1["deferred"] == 0:
            break
    ok("deep scan analyzes changed scopes without failures", deep1["failed"] == 0)
    with state.transaction() as tx:
        deep2 = reorganize_mod.reorganize_tree(tx, FakeLLM(), keys.audit_hmac_key, keys.excerpt_key)
    ok(
        "unchanged scopes are skipped on the next deep scan",
        deep2["scopes_run"] == 0 and deep2["skipped_unchanged"] == deep2["scopes_total"],
    )
    with state.transaction() as tx:
        tree.update_node(
            tx, keys.audit_hmac_key, merge_endpoints[1], "revise", body="合并后再补充一次修订"
        )
    with state.transaction() as tx:
        deep3 = reorganize_mod.reorganize_tree(tx, FakeLLM(), keys.audit_hmac_key, keys.excerpt_key)
    ok(
        "a subtree update re-arms the deep scan for its parent scope",
        deep3["scopes_run"] >= 1,
    )

    print("export")
    for fmt in export.FORMATS:
        res = export.run_export(state.conn, config.export_dir, fmt)
        ok(f"export {fmt}", Path(res["path"]).is_file() and res["nodes"] >= 4)
    md = next(config.export_dir.glob("*.md")).read_text(encoding="utf-8")
    ok("markdown contains node title", "组织管理" in md)
    encrypted = export.run_export(
        state.conn,
        config.export_dir,
        "markdown",
        encryption_passphrase="export-passphrase",
    )
    original_name, decrypted = export.decrypt_export(
        Path(encrypted["path"]).read_bytes(), "export-passphrase"
    )
    ok(
        "password-encrypted export round trip",
        not encrypted["plaintext"]
        and original_name.endswith(".md")
        and "组织管理" in decrypted.decode("utf-8"),
    )
    encrypted_path = Path(encrypted["path"])
    os.utime(encrypted_path, (1, 1))
    export.cleanup_expired(config.export_dir, 1)
    ok("encrypted export is never TTL-deleted", encrypted_path.is_file())
    ok(
        "all export artifacts are owner-only",
        all(path.stat().st_mode & 0o777 == 0o600 for path in config.export_dir.iterdir()),
    )

    print("backup")
    seal("尚未处理的备份消息", "backup-pending")
    result = backup.create_backup(
        state.conn, keys.sqlcipher_hex(), config.backup_dir, config.key_header_path, config.inbox_dir
    )
    verify = backup.verify_restore(Path(result["backup_dir"]), keys.sqlcipher_hex())
    ok("backup verified (incl. wrong-key rejection)", verify["ok"])
    ok(
        "backup manifest hashes sealed inbox files",
        any(name.startswith("inbox/") for name in result["files"]),
    )
    recovery_keys = keyring.unlock_with_recovery_pack(config.key_header_path, recovery)
    restored_dir = workdir / "restored"
    restored = backup.restore_backup(Path(result["backup_dir"]), restored_dir, recovery_keys.sqlcipher_hex())
    recovery_keys.wipe()
    ok(
        "recovery pack restores verified backup",
        restored["ok"]
        and (restored_dir / "simai.db").is_file()
        and (restored_dir / config.key_header_path.name).is_file(),
    )
    restored_inbox = restored_dir / "inbox"
    ok(
        "restored data uses owner-only permissions",
        restored_dir.stat().st_mode & 0o777 == 0o700
        and (restored_dir / "simai.db").stat().st_mode & 0o777 == 0o600
        and (restored_dir / config.key_header_path.name).stat().st_mode & 0o777 == 0o600
        and restored_inbox.stat().st_mode & 0o777 == 0o700
        and all(
            item.stat().st_mode & 0o777 == (0o700 if item.is_dir() else 0o600)
            for item in restored_inbox.rglob("*")
        ),
    )
    manifest_path = Path(result["backup_dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["nodes"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    try:
        backup.verify_restore(Path(result["backup_dir"]), keys.sqlcipher_hex())
        raise AssertionError("tampered backup manifest accepted")
    except backup.BackupError:
        ok("backup manifest tampering is authenticated")

    keyring.change_passphrase(config.key_header_path, PASS, "new secure passphrase")
    ok("passphrase change preserves header mode 0600", config.key_header_path.stat().st_mode & 0o777 == 0o600)
    keyring.change_passphrase(config.key_header_path, "new secure passphrase", PASS)

    print("audit & logs")
    n_audit = state.conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    ok("audit events recorded", n_audit >= 8)

    state.lock()
    ok("locked again", not state.is_unlocked)


if __name__ == "__main__":
    main()
