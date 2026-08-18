"""Agent expertise + self-assessment signal (TOC Phase B).

Two read endpoints over the routing data:

  GET /api/agent/expertise        — per-agent mindmap data: the "configured"
                                    domain roots an agent is declared to own,
                                    plus the "demonstrated" tags lit up by
                                    completed work.
  GET /api/agent/self-assessment  — Satyam's self-awareness signal: how often
                                    he correctly recognized a capability gap
                                    and routed it (learn or delegate). Never
                                    fungible with growth points.

Both are pure aggregations of tables already populated by the triage endpoint:
`agent_domains` (configured), `task_delegations` + `task_skill_requirements`
(demonstrated), `self_assessments` (the recognition signal).
"""

from app.db import _supabase_url
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import psycopg2

router = APIRouter()


@router.get("/api/agent/expertise")
def agent_expertise():
    """Per-agent expertise: configured roots + demonstrated (completed) tags.

    "configured" = the root tags an agent is declared to own (whole subtree).
    "demonstrated" = tags on work the agent actually completed (lit up).
    "pending" = delegated work still open; "stale" = untouched > 5 days.
    """
    url = _supabase_url()
    if not url:
        return JSONResponse({"error": "no supabase"}, status_code=503)
    try:
        con = psycopg2.connect(url, connect_timeout=8)
        cur = con.cursor()

        cur.execute("SELECT id, name FROM users WHERE user_type = 'agent' ORDER BY id")
        agents = {r[0]: r[1] for r in cur.fetchall()}

        # Configured roots per agent
        cur.execute(
            "SELECT d.agent_id, g.name FROM agent_domains d JOIN tags g ON g.id = d.tag_id"
        )
        configured = {}
        for agent_id, tag_name in cur.fetchall():
            configured.setdefault(agent_id, []).append(tag_name)

        # Demonstrated expertise: tags on completed delegated work, per agent
        cur.execute(
            "SELECT d.to_user, g.name, COUNT(*) AS n "
            "FROM task_delegations d "
            "JOIN task_skill_requirements r ON r.task_id = d.task_id "
            "JOIN tags g ON g.id = r.tag_id "
            "WHERE d.state = 'completed' "
            "GROUP BY d.to_user, g.name ORDER BY n DESC"
        )
        demonstrated = {}
        for agent_id, tag_name, n in cur.fetchall():
            demonstrated.setdefault(agent_id, []).append({"tag_name": tag_name, "completed": n})

        # Pending + stale delegated work per agent
        cur.execute(
            "SELECT d.to_user, "
            "       COUNT(*) FILTER (WHERE d.state = 'delegated' AND t.is_active) AS pending, "
            "       COUNT(*) FILTER (WHERE d.state = 'delegated' AND t.is_active "
            "           AND d.created_at < NOW() - INTERVAL '5 days') AS stale, "
            "       COUNT(*) FILTER (WHERE d.state = 'completed') AS completed "
            "FROM task_delegations d JOIN tasks t ON t.id = d.task_id "
            "GROUP BY d.to_user"
        )
        workload = {}
        for agent_id, pending, stale, completed in cur.fetchall():
            workload[agent_id] = {"pending": pending, "stale": stale, "completed": completed}

        con.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    result = []
    for agent_id, name in sorted(agents.items()):
        w = workload.get(agent_id, {})
        result.append({
            "agent_id": agent_id,
            "name": name,
            "configured": configured.get(agent_id, []),
            "demonstrated": demonstrated.get(agent_id, []),
            "completed": w.get("completed", 0),
            "pending": w.get("pending", 0),
            "stale": w.get("stale", 0),
        })

    return {"agents": result}


@router.get("/api/agent/self-assessment")
def self_assessment():
    """Satyam's self-awareness signal — recognizing capability gaps.

    Counts routing decisions and surfaces the recognition ratio. A "recognition"
    is any decision made on a gapped task (learn or delegate) — Satyam correctly
    identified work outside his current mindmap. "over_delegated" flags tasks he
    offloaded that actually had no gap. Informational only; never growth points.
    """
    url = _supabase_url()
    if not url:
        return JSONResponse({"error": "no supabase"}, status_code=503)
    try:
        con = psycopg2.connect(url, connect_timeout=8)
        cur = con.cursor()

        cur.execute(
            "SELECT decision, had_gap, COUNT(*) FROM self_assessments "
            "GROUP BY decision, had_gap ORDER BY decision"
        )
        rows = cur.fetchall()

        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(gap_magnitude), 0) FROM self_assessments"
        )
        _row = cur.fetchone()
        total, total_gap = (_row or (0, 0))

        cur.execute(
            "SELECT sa.task_id, t.title, sa.decision, sa.had_gap, sa.chosen_agent, sa.created_at "
            "FROM self_assessments sa JOIN tasks t ON t.id = sa.task_id "
            "ORDER BY sa.created_at DESC LIMIT 20"
        )
        recent = [
            {"task_id": r[0], "title": r[1], "decision": r[2], "had_gap": r[3],
             "chosen_agent": r[4],
             "created_at": r[5].isoformat() if r[5] else None}
            for r in cur.fetchall()
        ]
        con.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    counts = {}
    for decision, had_gap, n in rows:
        counts[f"{decision}_{'gap' if had_gap else 'nogap'}"] = n

    recognized = sum(n for (d, g, n) in rows if g)  # decisions on gapped tasks
    delegated = sum(n for (d, g, n) in rows if d == "delegate")
    learned = sum(n for (d, g, n) in rows if d == "learn")
    over_delegated = counts.get("delegate_nogap", 0)

    return {
        "summary": {
            "total_decisions": total or 0,
            "gaps_recognized": recognized,
            "delegated": delegated,
            "learn_commitments": learned,
            "over_delegated": over_delegated,
            "recognition_ratio": round(recognized / total, 2) if total else None,
            "total_gap_magnitude": round(float(total_gap or 0), 2),
        },
        "recent": recent,
    }


@router.get("/api/agent/tags/review")
def tags_review():
    """List auto-created tags — sprawl candidates for Gyani to consolidate.

    Every tag the triage LLM invented on the fly carries auto_created=TRUE.
    This surfaces them with reference counts (tasks, signatures, children) so a
    loose signature can be merged into the real tree instead of fragmenting it.
    """
    url = _supabase_url()
    if not url:
        return JSONResponse({"error": "no supabase"}, status_code=503)
    try:
        con = psycopg2.connect(url, connect_timeout=8)
        cur = con.cursor()
        cur.execute(
            "SELECT t.id, t.name, t.category, "
            "       (SELECT COUNT(*) FROM task_tags tt WHERE tt.tag_id = t.id) AS task_refs, "
            "       (SELECT COUNT(*) FROM task_skill_requirements r WHERE r.tag_id = t.id) AS sig_refs, "
            "       (SELECT COUNT(*) FROM tags c WHERE c.parent_tag_id = t.id) AS children, "
            "       (SELECT COUNT(*) FROM verifications v WHERE v.tag_id = t.id) AS exam_refs "
            "FROM tags t WHERE t.auto_created = TRUE ORDER BY t.id"
        )
        rows = cur.fetchall()
        con.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    tags = [
        {"tag_id": r[0], "name": r[1], "category": r[2],
         "task_refs": r[3], "sig_refs": r[4], "children": r[5], "exam_refs": r[6]}
        for r in rows
    ]
    return {"auto_created": tags, "count": len(tags)}


@router.post("/api/agent/tags/merge")
async def tags_merge(request: Request):
    """Merge an auto-created tag into an existing one (consolidate sprawl).

    Body: {"from_tag_id": int, "into_tag_id": int}
    Reassigns every reference from the orphan to the survivor (task_tags,
    task_skill_requirements, verifications, agent_domains, tag_dependencies,
    and child-tag parent links), then deletes the orphan.
    """
    body = await request.json()
    from_id = body.get("from_tag_id")
    into_id = body.get("into_tag_id")
    if not from_id or not into_id or from_id == into_id:
        return JSONResponse({"error": "distinct from_tag_id and into_tag_id required"}, status_code=400)

    url = _supabase_url()
    if not url:
        return JSONResponse({"error": "no supabase"}, status_code=503)

    try:
        con = psycopg2.connect(url, connect_timeout=8)
        cur = con.cursor()
        cur.execute("SELECT id FROM tags WHERE id IN (%s, %s)", (from_id, into_id))
        found = {r[0] for r in cur.fetchall()}
        if from_id not in found or into_id not in found:
            con.close()
            return JSONResponse({"error": "one or both tags not found"}, status_code=404)

        # Reassign every reference from -> into, guarding against FK collisions
        # by deleting the from-side rows where into-side already exists.
        cur.execute(
            "DELETE FROM task_tags WHERE tag_id = %s AND task_id IN "
            "(SELECT task_id FROM task_tags WHERE tag_id = %s)", (from_id, into_id))
        cur.execute("UPDATE task_tags SET tag_id = %s WHERE tag_id = %s", (into_id, from_id))

        cur.execute(
            "DELETE FROM task_skill_requirements WHERE tag_id = %s AND task_id IN "
            "(SELECT task_id FROM task_skill_requirements WHERE tag_id = %s)", (from_id, into_id))
        cur.execute("UPDATE task_skill_requirements SET tag_id = %s WHERE tag_id = %s", (into_id, from_id))

        cur.execute(
            "DELETE FROM verifications WHERE tag_id = %s AND task_id IN "
            "(SELECT task_id FROM verifications WHERE tag_id = %s)", (from_id, into_id))
        cur.execute("UPDATE verifications SET tag_id = %s WHERE tag_id = %s", (into_id, from_id))

        cur.execute("DELETE FROM agent_domains WHERE tag_id = %s AND agent_id IN "
                    "(SELECT agent_id FROM agent_domains WHERE tag_id = %s)", (from_id, into_id))
        cur.execute("UPDATE agent_domains SET tag_id = %s WHERE tag_id = %s", (into_id, from_id))

        cur.execute("DELETE FROM tag_dependencies WHERE tag_id = %s AND depends_on_tag_id = %s", (into_id, from_id))
        cur.execute("DELETE FROM tag_dependencies WHERE tag_id = %s AND depends_on_tag_id = %s", (from_id, into_id))
        cur.execute("UPDATE tag_dependencies SET tag_id = %s WHERE tag_id = %s", (into_id, from_id))
        cur.execute("UPDATE tag_dependencies SET depends_on_tag_id = %s WHERE depends_on_tag_id = %s", (into_id, from_id))

        # Re-parent the orphan's children onto the survivor
        cur.execute("UPDATE tags SET parent_tag_id = %s WHERE parent_tag_id = %s", (into_id, from_id))

        # Finally delete the orphan
        cur.execute("DELETE FROM tags WHERE id = %s", (from_id,))

        con.commit()
        con.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    return {"merged": True, "from_tag_id": from_id, "into_tag_id": into_id}

