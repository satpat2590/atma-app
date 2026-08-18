#!/usr/bin/env python3
"""Build the Omni mindmap — a true knowledge graph of personal growth.

Architecture:
  Tier 0: You (center anchor)
  Tier 1: Domains   (physical, mental, financial, social, edoras)
  Tier 2: Fields    (root tags: Health, Computer Science, Finance, ...)
  Tier 3: Subfields (child tags: Strength Training, NLP, Poetry, ...)
  Tier 4: Tasks     (leaves, only rendered at deep zoom by frontend)

Edges:
  - hierarchy: parent tag -> child tag
  - cross-pollination: subfields linked when a task shares both tags
  - membership: task -> its tags
"""
import os, sys

# Cron may invoke this via bare `python3` (ignoring shebang). If any required
# package is missing, re-exec under the conda interpreter which has them all.
try:
    import psycopg2  # noqa: F401
    import sklearn  # noqa: F401
    import numpy  # noqa: F401
except ImportError:
    conda = os.path.expanduser("~/miniconda3/bin/python3")
    if os.path.exists(conda) and os.path.realpath(conda) != os.path.realpath(sys.executable):
        os.execv(conda, [conda, os.path.abspath(__file__)] + sys.argv[1:])
    raise


import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HOME = Path.home()
OUT_DIR = HOME / "omni" / "static"
GRAPH_JSON = OUT_DIR / "graph.json"


def fetch_atma():
    url = None
    for p in ('.zshrc', '.bashrc'):
        path = os.path.expanduser('~/' + p)
        if not os.path.exists(path): continue
        for line in open(path):
            if 'DATABASE_URL' in line and 'supabase' in line:
                url = line.split('=', 1)[1].strip().strip('"\'')
                break
        if url: break
    if not url:
        raise RuntimeError("no supabase DATABASE_URL found")

    import psycopg2
    con = psycopg2.connect(url, connect_timeout=8)
    c = con.cursor()

    c.execute("select id, name, user_type from users")
    users = {r[0]: {"name": r[1], "type": r[2]} for r in c.fetchall()}

    c.execute("select id, name, parent_tag_id, category from tags")
    tags = [{"id": r[0], "name": r[1], "parent": r[2], "category": r[3]}
            for r in c.fetchall()]

    c.execute("""
        SELECT t.id, t.title, t.category, t.priority, t.assigned_to,
               t.owner_type, t.routing_state,
               array_remove(array_agg(tag.id), NULL) as tag_ids
        FROM tasks t
        LEFT JOIN task_tags tt ON t.id = tt.task_id
        LEFT JOIN tags tag ON tt.tag_id = tag.id
        GROUP BY t.id, t.title, t.category, t.priority, t.assigned_to,
                 t.owner_type, t.routing_state
    """)
    tasks = [{"id": r[0], "title": r[1], "category": r[2], "priority": r[3],
              "assigned_to": users.get(r[4], {"name": "?", "type": "unknown"}),
              "owner_type": r[5] or "satyam",
              "routing_state": r[6],
              "tag_ids": r[7] or []} for r in c.fetchall()]

    c.execute("""
        SELECT task_id, count(*), sum(points), max(completed_at)
        FROM task_completions GROUP BY task_id
    """)
    stats = {r[0]: {"completions": r[1], "points": r[2] or 0, "last": r[3]}
             for r in c.fetchall()}

    # windowed growth scores (SPEC-growth-system-revamp-2026-08)
    try:
        c.execute("""
            SELECT task_id, completions_28d, expected_28d, growth_score
            FROM growth_scores_28d
        """)
        growth = {r[0]: {"completions_28d": r[1], "expected_28d": float(r[2] or 1),
                         "growth_score": float(r[3] or 0)} for r in c.fetchall()}
    except Exception as e:
        print(f"  growth_scores_28d unavailable ({e}); falling back to zeros")
        growth = {}

    con.close()
    return users, tags, tasks, stats, growth


def embed(texts):
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer('all-MiniLM-L6-v2')
        return model.encode(texts, show_progress_bar=False)
    except ImportError:
        from sklearn.feature_extraction.text import TfidfVectorizer
        vec = TfidfVectorizer(max_features=384, stop_words='english')
        return vec.fit_transform(texts).toarray()


def main():
    print("Fetching Atma data...")
    users, tags, tasks, stats, growth = fetch_atma()
    print(f"  {len(tags)} tags, {len(tasks)} tasks")

    # PERSONAL MAP: Satyam's tasks only. Agent tasks (owner_type='agent',
    # i.e. delegated) belong to separate per-agent views, not the personal
    # growth graph. edoras-category tasks are agent trading output.
    tasks = [t for t in tasks
             if (t.get("owner_type") or "satyam") != "agent"
             and t["category"] != "edoras"]
    print(f"  {len(tasks)} tasks after personal filter (Satyam only)")

    tag_by_id = {t["id"]: t for t in tags}
    now = datetime.now(timezone.utc)

    # domain fold: non-root categories roll into financial (SPEC §4c)
    DOMAIN_FOLD = {"edoras": "financial", "whoop": "physical",
                   "omni": "mental", "veltiosi": "mental", "argus": "financial"}
    ROOT_DOMAINS = ("physical", "mental", "financial", "social")

    def fold(cat):
        return DOMAIN_FOLD.get(cat, cat) if cat in ROOT_DOMAINS or cat in DOMAIN_FOLD else "mental"

    def growth_of(task_id):
        return growth.get(task_id, {"completions_28d": 0, "expected_28d": 1.0, "growth_score": 0.0})

    # ---- aggregate stats up the tag tree ----
    # task stats roll up to every ancestor tag
    tag_agg = {t["id"]: {"points": 0, "completions": 0, "last": None, "tasks": 0,
                         "gs_num": 0.0, "gs_den": 0.0, "win_completions": 0}
               for t in tags}

    def ancestors(tag_id):
        chain = []
        cur = tag_id
        while cur is not None and cur in tag_by_id:
            chain.append(cur)
            cur = tag_by_id[cur]["parent"]
        return chain

    for task in tasks:
        s = stats.get(task["id"], {"completions": 0, "points": 0, "last": None})
        g = growth_of(task["id"])
        seen = set()
        for tid in task["tag_ids"]:
            for anc in ancestors(tid):
                if anc in seen: continue
                seen.add(anc)
                a = tag_agg[anc]
                a["points"] += s["points"]
                a["completions"] += s["completions"]
                a["tasks"] += 1
                a["gs_num"] += g["growth_score"] * g["expected_28d"]
                a["gs_den"] += g["expected_28d"]
                a["win_completions"] += g["completions_28d"]
                if s["last"] and (a["last"] is None or s["last"] > a["last"]):
                    a["last"] = s["last"]

    # tasks without tags still count toward their category domain
    domain_agg = defaultdict(lambda: {"points": 0, "completions": 0, "last": None, "tasks": 0,
                                      "gs_num": 0.0, "gs_den": 0.0, "win_completions": 0})
    for task in tasks:
        s = stats.get(task["id"], {"completions": 0, "points": 0, "last": None})
        g = growth_of(task["id"])
        d = domain_agg[fold(task["category"])]
        d["points"] += s["points"]
        d["completions"] += s["completions"]
        d["tasks"] += 1
        d["gs_num"] += g["growth_score"] * g["expected_28d"]
        d["gs_den"] += g["expected_28d"]
        d["win_completions"] += g["completions_28d"]
        if s["last"] and (d["last"] is None or s["last"] > d["last"]):
            d["last"] = s["last"]

    def warmth(last):
        if last is None: return 0.0
        days = (now - last).days
        return max(0.0, 1.0 - days / 30.0)  # 1.0 = today, 0 = 30+ days dormant

    def score_color(gs):
        return "green" if gs >= 1.0 else ("amber" if gs >= 0.4 else "red")

    # ---- build nodes ----
    nodes = []
    # tier 1: domains (exactly the 4 roots)
    for dom in ROOT_DOMAINS:
        d = domain_agg.get(dom, {"points": 0, "completions": 0, "last": None, "tasks": 0,
                                 "gs_num": 0.0, "gs_den": 0.0, "win_completions": 0})
        gs = round(d["gs_num"] / d["gs_den"], 2) if d["gs_den"] else 0.0
        nodes.append({
            "id": f"domain-{dom}", "label": dom.capitalize(), "tier": 1,
            "type": "domain", "category": dom,
            "points": d["points"], "completions": d["completions"],
            "task_count": d["tasks"], "warmth": round(warmth(d["last"]), 2),
            "growth_score": gs, "score_color": score_color(gs),
            "window_completions": d["win_completions"],
        })

    # tier 2: fields (root tags), tier 3: subfields (child tags)
    for t in tags:
        a = tag_agg[t["id"]]
        if a["tasks"] == 0:  # skip tags with no tasks under them
            continue
        tier = 2 if t["parent"] is None else 3
        gs = round(a["gs_num"] / a["gs_den"], 2) if a["gs_den"] else 0.0
        nodes.append({
            "id": f"tag-{t['id']}", "label": t["name"], "tier": tier,
            "type": "field" if tier == 2 else "subfield",
            "category": t["category"], "parent": t["parent"],
            "points": a["points"], "completions": a["completions"],
            "task_count": a["tasks"], "warmth": round(warmth(a["last"]), 2),
            "growth_score": gs, "score_color": score_color(gs),
            "window_completions": a["win_completions"],
        })

    # tier 4: task leaves
    for task in tasks:
        s = stats.get(task["id"], {"completions": 0, "points": 0, "last": None})
        g = growth_of(task["id"])
        gs = round(g["growth_score"], 2)
        nodes.append({
            "id": f"task-{task['id']}", "label": task["title"][:60], "tier": 4,
            "type": "task", "category": task["category"],
            "points": s["points"], "completions": s["completions"],
            "priority": task["priority"],
            "assigned_to": task["assigned_to"]["name"],
            "assigned_type": task["assigned_to"]["type"],
            "owner_type": task["owner_type"],
            "routing_state": task["routing_state"],
            "tag_ids": task["tag_ids"],
            "warmth": round(warmth(s["last"]), 2),
            "growth_score": gs, "score_color": score_color(gs),
            "expected": g["expected_28d"],
            "window_completions": g["completions_28d"],
        })

    # ---- edges ----
    edges = []
    # hierarchy: parent tag -> child tag
    node_ids = {n["id"] for n in nodes}
    for t in tags:
        if t["parent"] is not None and f"tag-{t['id']}" in node_ids and f"tag-{t['parent']}" in node_ids:
            edges.append({"source": f"tag-{t['parent']}", "target": f"tag-{t['id']}",
                          "kind": "hierarchy", "weight": 1})
    # domain -> root tag
    for t in tags:
        if t["parent"] is None and f"tag-{t['id']}" in node_ids and f"domain-{t['category']}" in node_ids:
            edges.append({"source": f"domain-{t['category']}", "target": f"tag-{t['id']}",
                          "kind": "hierarchy", "weight": 1})
    # cross-pollination: subfields sharing tasks
    for task in tasks:
        tids = [t for t in task["tag_ids"] if f"tag-{t}" in node_ids]
        for i in range(len(tids)):
            for j in range(i + 1, len(tids)):
                # only link across different root fields
                r1 = ancestors(tids[i])[-1] if ancestors(tids[i]) else tids[i]
                r2 = ancestors(tids[j])[-1] if ancestors(tids[j]) else tids[j]
                if r1 != r2:
                    edges.append({"source": f"tag-{tids[i]}", "target": f"tag-{tids[j]}",
                                  "kind": "cross", "weight": 1})
    # task membership
    for task in tasks:
        for tid in task["tag_ids"]:
            if f"tag-{tid}" in node_ids:
                edges.append({"source": f"task-{task['id']}", "target": f"tag-{tid}",
                              "kind": "membership", "weight": 1})
        # untagged tasks attach to domain
        if not task["tag_ids"] and f"domain-{task['category']}" in node_ids:
            edges.append({"source": f"task-{task['id']}", "target": f"domain-{task['category']}",
                          "kind": "membership", "weight": 1})

    # dedupe cross edges (keep count as weight)
    edge_counts = defaultdict(int)
    for e in edges:
        if e["kind"] == "cross":
            key = tuple(sorted([e["source"], e["target"]]))
            edge_counts[key] += 1
    edges = [e for e in edges if e["kind"] != "cross"]
    for (s, t), w in edge_counts.items():
        edges.append({"source": s, "target": t, "kind": "cross", "weight": w})

    print(f"  {len(nodes)} nodes, {len(edges)} edges "
          f"(hierarchy {sum(1 for e in edges if e['kind']=='hierarchy')}, "
          f"cross {sum(1 for e in edges if e['kind']=='cross')}, "
          f"membership {sum(1 for e in edges if e['kind']=='membership')})")

    # ---- semantic layout ----
    # embed field/subfield labels; domains anchored by mean child embedding
    print("Embedding for semantic layout...")
    field_nodes = [n for n in nodes if n["type"] in ("field", "subfield")]
    if field_nodes:
        X = np.array(embed([n["label"] for n in field_nodes]))
        X = np.nan_to_num(X)
        from sklearn.decomposition import PCA
        pos2 = PCA(n_components=2).fit_transform(X)
        pos2 = (pos2 - pos2.mean(axis=0)) / (pos2.std(axis=0) + 1e-9)

        for i, n in enumerate(field_nodes):
            n["x"] = float(pos2[i, 0])
            n["y"] = float(pos2[i, 1])

        # domains at centroid of their fields
        for n in nodes:
            if n["type"] == "domain":
                kids = [f for f in field_nodes if f["category"] == n["category"]]
                if kids:
                    n["x"] = float(np.mean([k["x"] for k in kids]))
                    n["y"] = float(np.mean([k["y"] for k in kids]))
                else:
                    n["x"], n["y"] = 0.0, 0.0

        # light force pass on fields only: cross edges attract, all repel slightly
        idx = {n["id"]: i for i, n in enumerate(field_nodes)}
        cross = [(idx[e["source"]], idx[e["target"]], e["weight"])
                 for e in edges if e["kind"] == "cross"
                 and e["source"] in idx and e["target"] in idx]
        hier = [(idx[e["source"]], idx[e["target"]], e["weight"])
                for e in edges if e["kind"] == "hierarchy"
                and e["source"] in idx and e["target"] in idx]
        P = np.array([[n["x"], n["y"]] for n in field_nodes])
        for _ in range(150):
            # weak global repulsion
            diff = P[:, None, :] - P[None, :, :]
            d2 = np.clip((diff ** 2).sum(-1), 0.01, 1e4)
            f = np.clip(0.5 / d2, 0, 2)
            np.fill_diagonal(f, 0)
            rep = (diff * f[:, :, None]).sum(axis=1) * 0.01
            att = np.zeros_like(P)
            # hierarchy attraction (keep children near parents)
            for i, j, w in hier:
                d = P[j] - P[i]
                att[i] += d * 0.02
                att[j] -= d * 0.02
            # cross edge attraction (weaker)
            for i, j, w in cross:
                d = P[j] - P[i]
                att[i] += d * 0.005 * w
                att[j] -= d * 0.005 * w
            P += rep + att
            P = np.clip(P, -6, 6)
        for i, n in enumerate(field_nodes):
            n["x"] = round(float(P[i, 0]) * 100, 1)
            n["y"] = round(float(P[i, 1]) * 100, 1)
        # re-anchor domains after force pass
        for n in nodes:
            if n["type"] == "domain":
                kids = [f for f in field_nodes if f["category"] == n["category"]]
                if kids:
                    n["x"] = round(float(np.mean([k["x"] for k in kids])), 1)
                    n["y"] = round(float(np.mean([k["y"] for k in kids])), 1)

    # tasks: place near centroid of their tags (computed after field layout)
    tag_pos = {n["id"]: (n["x"], n["y"]) for n in nodes if n["type"] in ("field", "subfield")}
    dom_pos = {n["id"]: (n["x"], n["y"]) for n in nodes if n["type"] == "domain"}
    rng = np.random.default_rng(42)
    for n in nodes:
        if n["type"] == "task":
            pts = [tag_pos[f"tag-{t}"] for t in n["tag_ids"] if f"tag-{t}" in tag_pos]
            if pts:
                cx = sum(p[0] for p in pts) / len(pts)
                cy = sum(p[1] for p in pts) / len(pts)
            else:
                cx, cy = dom_pos.get(f"domain-{n['category']}", (0, 0))
            n["x"] = round(cx + float(rng.normal(0, 8)), 1)
            n["y"] = round(cy + float(rng.normal(0, 8)), 1)
            n.pop("tag_ids", None)

    graph = {"nodes": nodes, "edges": edges}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    GRAPH_JSON.write_text(json.dumps(graph, separators=(",", ":")))
    size_kb = GRAPH_JSON.stat().st_size // 1024
    print(f"Wrote {GRAPH_JSON} ({size_kb}KB)")


if __name__ == "__main__":
    main()
