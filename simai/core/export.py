"""Exports (section 18): simai.json, Markdown, OPML, GraphML, JSON Canvas.

Web-only operation. Plaintext exports get a TTL and are logged with scope
and file hash only - never with content.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from nacl import pwhash
from nacl.bindings import (
    crypto_aead_xchacha20poly1305_ietf_decrypt,
    crypto_aead_xchacha20poly1305_ietf_encrypt,
)

from ..db.engine import now_iso
from . import ids, tree

FORMATS = ("simai-json", "markdown", "opml", "graphml", "json-canvas")


class ExportError(Exception):
    pass


def run_export(
    conn,
    export_dir: Path,
    fmt: str,
    *,
    root_id: str | None = None,
    include_history: bool = False,
    include_ai_relations: bool = True,
    node_types: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    encryption_passphrase: str | None = None,
) -> dict:
    if fmt not in FORMATS:
        raise ExportError(f"Unknown format: {fmt} (expected one of {FORMATS})")

    snapshot = tree.export_snapshot(conn, root_id, include_history)
    nodes = snapshot["nodes"]
    keep = {n["id"] for n in nodes}
    if node_types:
        keep &= {n["id"] for n in nodes if n["node_type"] in node_types}
    if since:
        keep &= {n["id"] for n in nodes if n["updated_at"] >= since}
    if until:
        keep &= {n["id"] for n in nodes if n["updated_at"] <= until}
    if node_types or since or until:
        nodes = _keep_with_ancestors(nodes, keep)
    node_ids = {n["id"] for n in nodes}
    rels = [
        r
        for r in snapshot["relations"]
        if r["from_node_id"] in node_ids
        and r["to_node_id"] in node_ids
        and (include_ai_relations or r["state"] == "confirmed")
    ]

    renderers = {
        "simai-json": _render_json,
        "markdown": _render_markdown,
        "opml": _render_opml,
        "graphml": _render_graphml,
        "json-canvas": _render_canvas,
    }
    extensions = {
        "simai-json": "simai.json",
        "markdown": "md",
        "opml": "opml",
        "graphml": "graphml",
        "json-canvas": "canvas",
    }
    content = renderers[fmt](nodes, rels, include_history)

    export_dir.mkdir(parents=True, exist_ok=True)
    export_dir.chmod(0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    plain_name = f"simai-export-{stamp}-{secrets.token_hex(4)}.{extensions[fmt]}"
    if encryption_passphrase:
        if len(encryption_passphrase) < 8:
            raise ExportError("Export encryption passphrase must be at least 8 characters")
        path = export_dir / f"{plain_name}.enc"
        payload = _encrypt_content(content.encode("utf-8"), encryption_passphrase, plain_name)
        _owner_only_write(path, payload)
        plaintext = False
        file_hash = hashlib.sha256(payload).hexdigest()
    else:
        path = export_dir / plain_name
        _owner_only_write(path, content.encode("utf-8"))
        plaintext = True
        file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    export_id = ids.export_id()
    scope = {
        "root_id": root_id,
        "include_history": include_history,
        "include_ai_relations": include_ai_relations,
        "node_types": node_types,
        "since": since,
        "until": until,
        "nodes": len(nodes),
        "relations": len(rels),
    }
    conn.execute(
        """INSERT INTO export_log (id, scope_json, format, file_hash, plaintext, created_at)
           VALUES (?,?,?,?,?,?)""",
        (export_id, json.dumps(scope, ensure_ascii=False), fmt, file_hash, int(plaintext), now_iso()),
    )
    return {
        "export_id": export_id,
        "path": str(path),
        "file_hash": file_hash,
        "nodes": len(nodes),
        "relations": len(rels),
        "plaintext": plaintext,
    }


def _encrypt_content(content: bytes, passphrase: str, original_name: str) -> bytes:
    """Portable password-encrypted JSON envelope; no plaintext temp file."""
    salt = secrets.token_bytes(pwhash.argon2id.SALTBYTES)
    ops = pwhash.argon2id.OPSLIMIT_MODERATE
    mem = pwhash.argon2id.MEMLIMIT_MODERATE
    key = pwhash.argon2id.kdf(32, passphrase.encode("utf-8"), salt, opslimit=ops, memlimit=mem)
    nonce = secrets.token_bytes(24)
    aad = b"simai-export-v1"
    ciphertext = crypto_aead_xchacha20poly1305_ietf_encrypt(content, aad, nonce, key)
    envelope = {
        "format": "simai-encrypted-export",
        "version": 1,
        "original_name": original_name,
        "kdf": "argon2id",
        "opslimit": int(ops),
        "memlimit": int(mem),
        "salt": base64.b64encode(salt).decode("ascii"),
        "algorithm": "xchacha20-poly1305",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")


def decrypt_export(payload: bytes, passphrase: str) -> tuple[str, bytes]:
    """Decrypt an encrypted export envelope for restore/interoperability tests."""
    try:
        envelope = json.loads(payload)
        if envelope["format"] != "simai-encrypted-export" or envelope["version"] != 1:
            raise ValueError("unsupported format")
        if (
            envelope.get("kdf") != "argon2id"
            or int(envelope["opslimit"]) != int(pwhash.argon2id.OPSLIMIT_MODERATE)
            or int(envelope["memlimit"]) != int(pwhash.argon2id.MEMLIMIT_MODERATE)
        ):
            raise ValueError("unsupported or unsafe KDF parameters")
        key = pwhash.argon2id.kdf(
            32,
            passphrase.encode("utf-8"),
            base64.b64decode(envelope["salt"]),
            opslimit=int(envelope["opslimit"]),
            memlimit=int(envelope["memlimit"]),
        )
        content = crypto_aead_xchacha20poly1305_ietf_decrypt(
            base64.b64decode(envelope["ciphertext"]),
            b"simai-export-v1",
            base64.b64decode(envelope["nonce"]),
            key,
        )
        return envelope["original_name"], content
    except Exception as exc:
        raise ExportError("Cannot decrypt export: wrong passphrase or corrupt file") from exc


def cleanup_expired(export_dir: Path, ttl_minutes: int) -> int:
    """Delete plaintext export files older than the TTL (section 18.3)."""
    if not export_dir.is_dir() or ttl_minutes <= 0:
        return 0
    horizon = time.time() - ttl_minutes * 60
    removed = 0
    for f in export_dir.iterdir():
        # Password-encrypted packages are user-owned durable artifacts.  Only
        # Simai's known plaintext export names are eligible for TTL cleanup.
        if (
            f.is_file()
            and f.name.startswith("simai-export-")
            and not f.name.endswith(".enc")
            and f.stat().st_mtime < horizon
        ):
            f.unlink()
            removed += 1
    return removed


def _owner_only_write(path: Path, payload: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


# -- helpers ----------------------------------------------------------------


def _keep_with_ancestors(nodes: list[dict], keep: set[str]) -> list[dict]:
    by_id = {n["id"]: n for n in nodes}
    result = set(keep)
    for nid in keep:
        cur = by_id.get(nid)
        while cur and cur["parent_id"]:
            result.add(cur["parent_id"])
            cur = by_id.get(cur["parent_id"])
    return [n for n in nodes if n["id"] in result]


def _children_map(nodes: list[dict]) -> dict[str | None, list[dict]]:
    out: dict[str | None, list[dict]] = {}
    ids_present = {n["id"] for n in nodes}
    for n in nodes:
        parent = n["parent_id"] if n["parent_id"] in ids_present else None
        out.setdefault(parent, []).append(n)
    return out


# -- renderers ---------------------------------------------------------------


def _render_json(nodes, rels, include_history) -> str:
    return json.dumps(
        {
            "format": "simai.json",
            "version": 1,
            "exported_at": now_iso(),
            "nodes": nodes,
            "relations": rels,
        },
        ensure_ascii=False,
        indent=2,
    )


def _render_markdown(nodes, rels, include_history) -> str:
    children = _children_map(nodes)
    lines = ["# 思脉导出", "", f"导出时间：{now_iso()}", ""]

    def walk(parent, depth):
        for n in children.get(parent, []):
            lines.append(
                f"{'#' * min(depth + 2, 6)} {n['title']}  `{n['id']}` (rev {n['revision_no']}, {n['node_type']})"
            )
            if n["body"]:
                lines.append("")
                lines.append(n["body"])
            if include_history and n.get("history"):
                lines.append("")
                lines.append("<details><summary>历史版本</summary>")
                for h in n["history"]:
                    lines.append(
                        f"- rev {h['revision_no']} [{h['change_type']}] {h['created_at']}: {h['title']}"
                    )
                lines.append("</details>")
            lines.append("")
            walk(n["id"], depth + 1)

    walk(None, 0)
    if rels:
        lines += ["## 语义关系", ""]
        for r in rels:
            marker = "已确认" if r["state"] == "confirmed" else "AI 生成"
            lines.append(
                f"- `{r['from_node_id']}` --{r['relation_type']}--> `{r['to_node_id']}` "
                f"（{marker}）{('：' + r['rationale']) if r.get('rationale') else ''}"
            )
    return "\n".join(lines)


def _render_opml(nodes, rels, include_history) -> str:
    children = _children_map(nodes)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        f"<head><title>Simai Export</title><dateCreated>{now_iso()}</dateCreated></head>",
        "<body>",
    ]

    def walk(parent):
        for n in children.get(parent, []):
            attrs = f"text={quoteattr(n['title'])} _note={quoteattr(n['body'] or '')} simaiId={quoteattr(n['id'])}"
            kids = children.get(n["id"], [])
            if kids:
                out.append(f"<outline {attrs}>")
                walk(n["id"])
                out.append("</outline>")
            else:
                out.append(f"<outline {attrs}/>")

    walk(None)
    out += ["</body>", "</opml>"]
    return "\n".join(out)


def _render_graphml(nodes, rels, include_history) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '<key id="d_title" for="node" attr.name="title" attr.type="string"/>',
        '<key id="d_type" for="node" attr.name="node_type" attr.type="string"/>',
        '<key id="d_body" for="node" attr.name="body" attr.type="string"/>',
        '<key id="d_rel" for="edge" attr.name="relation_type" attr.type="string"/>',
        '<key id="d_state" for="edge" attr.name="state" attr.type="string"/>',
        '<key id="d_conf" for="edge" attr.name="confidence" attr.type="double"/>',
        '<graph id="simai" edgedefault="directed">',
    ]
    ids_present = {n["id"] for n in nodes}
    for n in nodes:
        out.append(
            f'<node id="{escape(n["id"])}">'
            f'<data key="d_title">{escape(n["title"])}</data>'
            f'<data key="d_type">{escape(n["node_type"])}</data>'
            f'<data key="d_body">{escape(n["body"] or "")}</data>'
            "</node>"
        )
    edge_no = 0
    for n in nodes:  # tree structure exported as transient `contains` edges
        if n["parent_id"] in ids_present:
            out.append(
                f'<edge id="t{edge_no}" source="{escape(n["parent_id"])}" target="{escape(n["id"])}">'
                '<data key="d_rel">contains</data><data key="d_state">structure</data></edge>'
            )
            edge_no += 1
    for r in rels:
        conf = r["confidence"] if r["confidence"] is not None else 0.0
        out.append(
            f'<edge id="r{edge_no}" source="{escape(r["from_node_id"])}" target="{escape(r["to_node_id"])}"'
            f"{'' if r['is_directed'] else ' directed="false"'}>"
            f'<data key="d_rel">{escape(r["relation_type"])}</data>'
            f'<data key="d_state">{escape(r["state"])}</data>'
            f'<data key="d_conf">{conf}</data></edge>'
        )
        edge_no += 1
    out += ["</graph>", "</graphml>"]
    return "\n".join(out)


def _render_canvas(nodes, rels, include_history) -> str:
    """JSON Canvas 1.0: simple tree-layered layout."""
    children = _children_map(nodes)
    canvas_nodes, edges = [], []
    y_by_depth: dict[int, int] = {}

    def walk(parent, depth):
        for n in children.get(parent, []):
            y = y_by_depth.get(depth, 0)
            y_by_depth[depth] = y + 140
            canvas_nodes.append(
                {
                    "id": n["id"],
                    "type": "text",
                    "text": f"**{n['title']}**\n{(n['body'] or '')[:280]}",
                    "x": depth * 420,
                    "y": y,
                    "width": 360,
                    "height": 120,
                }
            )
            if parent is not None:
                edges.append(
                    {
                        "id": f"t-{n['id']}",
                        "fromNode": parent,
                        "toNode": n["id"],
                        "fromSide": "right",
                        "toSide": "left",
                    }
                )
            walk(n["id"], depth + 1)

    walk(None, 0)
    for r in rels:
        edges.append(
            {
                "id": r["id"],
                "fromNode": r["from_node_id"],
                "toNode": r["to_node_id"],
                "label": r["relation_type"],
                "color": "4" if r["state"] == "confirmed" else "6",
            }
        )
    return json.dumps({"nodes": canvas_nodes, "edges": edges}, ensure_ascii=False, indent=2)
