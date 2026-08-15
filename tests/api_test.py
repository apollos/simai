"""In-process API test for the Simai web service (no real network needed).

Run:  python tests/api_test.py
Uses a temporary data directory; the OpenClaw gateway is unreachable so
model-backed endpoints must fail explicitly (never silently) while the
raw/keyword paths keep working.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml
from fastapi.testclient import TestClient

from simai.api.app import create_app
from simai.config import load_config
from simai.core import capture as capture_mod
from simai.core import export as export_mod

PASS = "test-pass-12345"

checks: list[str] = []


def ok(name: str, cond: bool = True) -> None:
    if not cond:
        print(f"FAIL {name}")
        sys.exit(1)
    checks.append(name)
    print(f"  ok {name}")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="simai-api-test-"))
    try:
        run(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f"\nAll {len(checks)} checks passed.")


def run(workdir: Path) -> None:
    unix_socket_available = _unix_socket_available(workdir)
    cfg_path = workdir / "simai.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "runtime": {"profile": "local_wsl", "data_dir": str(workdir / "data")},
                "profiles": {"local_wsl": {"openclaw_gateway": "http://127.0.0.1:1"}},
                "source_bindings": [
                    {
                        "id": "local_web",
                        "channel": "webchat",
                        "account_id": "local",
                        "sender_key": "owner",
                        "enabled": True,
                        "passive_capture": True,
                    },
                    {
                        "id": "other_web",
                        "channel": "webchat",
                        "account_id": "other",
                        "sender_key": "other-owner",
                        "enabled": True,
                        "passive_capture": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(cfg_path)
    app = create_app(config)
    plugin_headers = {"X-Simai-Plugin-Token": config.plugin_token_path.read_text(encoding="ascii").strip()}
    ok("plugin credential file is owner-only", os.stat(config.plugin_token_path).st_mode & 0o777 == 0o600)

    with TestClient(app) as client:
        print("lock state & sessions")
        st = client.get("/api/status").json()
        ok("starts locked and uninitialized", st["locked"] and not st["initialized"])
        ok("protected route requires session", client.get("/api/tree").status_code == 401)
        ok("console page served", "<html" in client.get("/").text.lower())

        print("init")
        r = client.post("/api/init", json={"passphrase": PASS})
        ok(
            "init returns recovery pack",
            r.status_code == 200 and "vault_root_key" in r.json()["recovery_pack"],
        )
        ok("init is one-time", client.post("/api/init", json={"passphrase": PASS}).status_code == 409)
        st = client.get("/api/status").json()
        ok("unlocked after init", not st["locked"] and st["initialized"])
        ok(
            "ingress starts after web init"
            if unix_socket_available
            else "ingress start skipped (test sandbox denies AF_UNIX)",
            bool(getattr(app.state.ingress, "listening", False)) or not unix_socket_available,
        )

        # Regression: /api/unlock is an authentication boundary even while
        # another session keeps the process-wide vault open.
        client.cookies.clear()
        public_status = client.get("/api/status").json()
        ok(
            "public status hides vault metadata",
            set(public_status) == {"locked", "initialized"},
        )
        r = client.post("/api/unlock", json={"passphrase": "wrong-while-open"})
        ok("wrong passphrase rejected while already unlocked", r.status_code == 403)
        ok("failed unlock grants no session", client.get("/api/tree").status_code == 401)
        r = client.post("/api/login", json={"passphrase": PASS})
        ok("correct re-login while unlocked", r.status_code == 200)

        print("capture -> confirm")
        r = client.post("/api/capture", json={"text": "记录一下：产品要聚焦单用户体验", "use_model": False})
        ok("raw capture creates candidate", r.status_code == 200 and r.json()["cards"])
        cand_id = r.json()["cards"][0]["candidate_id"]
        r = client.get("/api/candidates")
        ok("pending candidate listed", any(c["candidate_id"] == cand_id for c in r.json()["candidates"]))
        r = client.post(f"/api/candidates/{cand_id}/confirm", json={"action": "create_root"})
        ok("confirm writes node", r.status_code == 200 and r.json()["node_id"])
        node_id = r.json()["node_id"]
        ok(
            "candidate gone after confirm",
            all(c["candidate_id"] != cand_id for c in client.get("/api/candidates").json()["candidates"]),
        )

        print("model outage is explicit")
        r = client.post("/api/capture", json={"text": "测试模型不可用", "use_model": True})
        ok("model capture fails loudly (502)", r.status_code == 502)
        r = client.post("/api/query", json={"question": "没有任何命中的问题词"})
        ok(
            "query with no hits answers honestly (no model call)",
            r.status_code == 200 and r.json()["citations"] == [],
        )
        r = client.post("/api/query", json={"question": "单用户"})
        ok("query with hits fails loudly when model down (502)", r.status_code == 502)

        print("tree & relations")
        r = client.post("/api/nodes", json={"title": "组织管理", "body": "顶层主题", "node_type": "topic"})
        root2 = r.json()["node_id"]
        r = client.get("/api/tree")
        ok("tree lists both nodes", {node_id, root2} <= {n["id"] for n in r.json()["nodes"]})
        r = client.get(f"/api/nodes/{node_id}")
        d = r.json()
        ok("node detail has body/path/history", d["body"] and d["path"] and len(d["history"]) == 1)
        r = client.post(
            "/api/relations",
            json={
                "from_node_id": node_id,
                "to_node_id": root2,
                "relation_type": "related_to",
                "rationale": "测试关系",
            },
        )
        ok("user relation created", r.status_code == 200)
        rel_id = r.json()["relation_id"]
        g = client.get("/api/relations/graph", params={"node_id": node_id}).json()
        ok("relation appears in local graph", any(e["id"] == rel_id for e in g["relations"]))
        ok(
            "move refused into own subtree",
            client.post(f"/api/nodes/{root2}/move", json={"new_parent_id": root2}).status_code == 400,
        )

        print("search & export")
        hits = client.post("/api/search", json={"q": "单用户"}).json()["results"]
        ok("keyword search finds node", any(h["node_id"] == node_id for h in hits))
        r = client.post("/api/export", json={"format": "markdown"})
        ok("markdown export", r.status_code == 200 and r.json()["nodes"] >= 2)
        fname = Path(r.json()["path"]).name
        r = client.get(f"/api/export/{fname}")
        ok("export downloadable", r.status_code == 200 and "组织管理" in r.text)
        ok("export path traversal rejected", client.get("/api/export/..%2Fsimai.yaml").status_code == 404)
        r = client.post(
            "/api/export", json={"format": "markdown", "encryption_passphrase": "export-passphrase"}
        )
        encrypted_path = Path(r.json()["path"])
        _, decrypted = export_mod.decrypt_export(encrypted_path.read_bytes(), "export-passphrase")
        ok(
            "encrypted export API writes no plaintext result",
            r.status_code == 200 and not r.json()["plaintext"] and "组织管理" in decrypted.decode("utf-8"),
        )

        print("personal dictionary")
        r = client.post("/api/dictionary", json={"term": "思脉", "note": "产品名"})
        ok("dictionary term added", r.status_code == 200)
        terms = client.get("/api/dictionary").json()["terms"]
        ok("dictionary term listed", any(t["term"] == "思脉" for t in terms))
        r = client.delete("/api/dictionary/思脉")
        ok(
            "dictionary term removed",
            r.status_code == 200
            and not any(t["term"] == "思脉" for t in client.get("/api/dictionary").json()["terms"]),
        )

        print("plugin api")
        with app.state.vault.transaction() as tx:
            other_card = capture_mod.create_raw_candidate(
                tx,
                app.state.vault.keys.excerpt_key,
                "另一个来源的候选",
                source_binding_id="other_web",
            )
        ok(
            "plugin API rejects missing credential",
            client.get("/plugin-api/status", params={"binding_id": "local_web"}).status_code == 401,
        )
        ok(
            "plugin status with valid binding",
            client.get(
                "/plugin-api/status", params={"binding_id": "local_web"}, headers=plugin_headers
            ).status_code
            == 200,
        )
        ok(
            "plugin rejects unknown binding",
            client.get(
                "/plugin-api/status", params={"binding_id": "evil"}, headers=plugin_headers
            ).status_code
            == 403,
        )
        listing = client.get(
            "/plugin-api/candidates",
            params={"binding_id": "local_web"},
            headers=plugin_headers,
        ).json()["candidates"]
        ok(
            "plugin candidate list is isolated by binding",
            all(c["candidate_id"] != other_card["candidate_id"] for c in listing),
        )
        r = client.post(
            f"/plugin-api/candidates/{other_card['candidate_id']}/decide",
            headers=plugin_headers,
            json={"binding_id": "local_web", "decision": "reject"},
        )
        ok("plugin cannot decide another binding candidate", r.status_code == 403)
        # POST bodies must parse (regression: function-local models were
        # invisible to FastAPI under `from __future__ import annotations`)
        r = client.post(
            "/plugin-api/query",
            headers=plugin_headers,
            json={"binding_id": "local_web", "question": "单用户"},
        )
        ok("plugin query body parsed (502 = model down, not 422)", r.status_code == 502)
        r = client.post(
            "/plugin-api/capture", headers=plugin_headers, json={"binding_id": "local_web", "text": "测试"}
        )
        ok("plugin capture body parsed (502 = model down, not 422)", r.status_code == 502)
        r = client.post("/plugin-api/daily/run", headers=plugin_headers, json={"binding_id": "local_web"})
        ok("plugin daily run works with empty inbox", r.status_code == 200 and r.json()["processed"] == 0)

        print("lock / unlock cycle")
        stale_session = app.state.sessions.create()
        r = client.post("/api/lock")
        ok("manual lock", r.status_code == 200 and r.json()["locked"])
        ok("locked db returns 423 after re-login", client.get("/api/status").json()["locked"])
        r = client.post("/api/unlock", json={"passphrase": "wrong-password-1"})
        ok("wrong passphrase rejected (403)", r.status_code == 403)
        r = client.post("/api/unlock", json={"passphrase": PASS})
        ok("unlock again", r.status_code == 200 and not r.json()["locked"])
        new_session = r.cookies.get("simai_session")
        client.cookies.clear()
        client.cookies.set("simai_session", stale_session)
        ok("global lock invalidates every prior web session", client.get("/api/tree").status_code == 401)
        client.cookies.clear()
        client.cookies.set("simai_session", new_session)
        ok("tree readable after unlock", client.get("/api/tree").status_code == 200)


def _unix_socket_available(workdir: Path) -> bool:
    if not hasattr(socket, "AF_UNIX"):
        return False
    path = workdir / "socket-probe.sock"
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
