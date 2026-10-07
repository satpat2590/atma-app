"""Clean up test data from the self-assessment verification run.

Deletes test tasks 59, 61 (+ curriculum sub-tasks 62-64), the stale task 66
from a prior test, and the auto-created 'Verification' tag from the Atma
Supabase, leaving the DB clean.

Usage:
    python3 scripts/cleanup_test_data.py
"""
import re
import psycopg2

env_path = "/home/satyamini/.hermes/profiles/gyani/.env"
env = open(env_path).read()
url = re.search(r'ATMA_DATABASE_URL="?([^"\n]+)', env).group(1).strip()

con = psycopg2.connect(url, connect_timeout=10)
cur = con.cursor()

# Collect ALL test-related task IDs
all_task_ids = {59, 61, 62, 63, 64, 66}  # 66 = stale prior test task

# Also get any task_ids from self_assessments (in case there are others)
cur.execute("SELECT task_id FROM self_assessments")
for r in cur.fetchall():
    all_task_ids.add(r[0])

# Check for auto-created 'Verification' tag
cur.execute("SELECT id FROM tags WHERE name = 'Verification'")
vtag_row = cur.fetchone()
vtag_id = vtag_row[0] if vtag_row else None

print(f"Task IDs to delete: {sorted(all_task_ids)}")
if vtag_id:
    print(f"Verification tag to delete: id={vtag_id}")

# Delete in FK-safe order
for tid in all_task_ids:
    cur.execute("DELETE FROM self_assessments WHERE task_id = %s", (tid,))
    cur.execute("DELETE FROM task_delegations WHERE task_id = %s", (tid,))
    cur.execute("DELETE FROM task_skill_requirements WHERE task_id = %s", (tid,))
    cur.execute("DELETE FROM task_tags WHERE task_id = %s", (tid,))
    cur.execute("DELETE FROM task_completions WHERE task_id = %s", (tid,))
    cur.execute("DELETE FROM tasks WHERE id = %s", (tid,))

if vtag_id:
    # Remove any remaining task_tags referencing this tag
    cur.execute("DELETE FROM task_tags WHERE tag_id = %s", (vtag_id,))
    cur.execute(
        "DELETE FROM tag_dependencies WHERE tag_id = %s OR depends_on_tag_id = %s",
        (vtag_id, vtag_id),
    )
    cur.execute("DELETE FROM agent_domains WHERE tag_id = %s", (vtag_id,))
    cur.execute("DELETE FROM tags WHERE id = %s", (vtag_id,))
    print(f"Deleted Verification tag id={vtag_id}")

con.commit()

cur.execute("SELECT COUNT(*) FROM self_assessments")
print(f"self_assessments after: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM task_delegations")
print(f"task_delegations after: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM task_skill_requirements")
print(f"task_skill_requirements after: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM tags WHERE name = 'Verification'")
print(f"Verification tag exists: {cur.fetchone()[0] > 0}")
con.close()
print("Cleanup complete.")
