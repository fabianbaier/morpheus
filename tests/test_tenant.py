import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import context, db, tenant


class TenantTest(unittest.TestCase):
    def test_resolve_project_root_uses_nearest_project_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project-a"
            nested = project / "src" / "pkg"
            nested.mkdir(parents=True)
            (project / "pyproject.toml").write_text("[project]\nname='a'\n", encoding="utf-8")

            resolved, root_kind = tenant.resolve_project_root(nested)

        self.assertEqual(resolved, project.resolve())
        self.assertEqual(root_kind, "marker")

    def test_db_filters_live_and_memory_rows_by_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db"
            project_a = db.ProjectTenant(
                tenant_id=tenant.tenant_id_for_root(root / "a"),
                name="a",
                root_path=str(root / "a"),
                root_kind="cwd",
            )
            project_b = db.ProjectTenant(
                tenant_id=tenant.tenant_id_for_root(root / "b"),
                name="b",
                root_path=str(root / "b"),
                root_kind="cwd",
            )

            with patch.object(db, "DB_DIR", db_dir), patch.object(db, "DB_PATH", db_dir / "morpheus.db"):
                project_a = db.upsert_project_tenant(project_a)
                project_b = db.upsert_project_tenant(project_b)
                mission_a = db.Mission(
                    tab_id="tab-a",
                    tenant_id=project_a.tenant_id,
                    project_root=project_a.root_path,
                    goal="project a",
                )
                mission_b = db.Mission(
                    tab_id="tab-b",
                    tenant_id=project_b.tenant_id,
                    project_root=project_b.root_path,
                    goal="project b",
                )
                db.upsert(mission_a)
                db.upsert(mission_b)

                missions_a = db.all_missions(tenant_id=project_a.tenant_id)
                memories_a = db.all_memory(include_archived=True, tenant_id=project_a.tenant_id)

        self.assertEqual([mission.tab_id for mission in missions_a], ["tab-a"])
        self.assertEqual([memory.mission_id for memory in memories_a], [mission_a.mission_id])

    def test_context_can_be_tenant_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db"
            with patch.object(db, "DB_DIR", db_dir), patch.object(db, "DB_PATH", db_dir / "morpheus.db"):
                project_a = db.upsert_project_tenant(
                    db.ProjectTenant(
                        tenant_id=tenant.tenant_id_for_root(root / "a"),
                        name="a",
                        root_path=str(root / "a"),
                        root_kind="cwd",
                    )
                )
                project_b = db.upsert_project_tenant(
                    db.ProjectTenant(
                        tenant_id=tenant.tenant_id_for_root(root / "b"),
                        name="b",
                        root_path=str(root / "b"),
                        root_kind="cwd",
                    )
                )
                db.upsert(
                    db.Mission(
                        tab_id="tab-a",
                        tenant_id=project_a.tenant_id,
                        project_root=project_a.root_path,
                        goal="alpha",
                    )
                )
                db.upsert(
                    db.Mission(
                        tab_id="tab-b",
                        tenant_id=project_b.tenant_id,
                        project_root=project_b.root_path,
                        goal="beta",
                    )
                )

                text = context.build_markdown(tenant_id=project_a.tenant_id)
                short = context.build_short(tenant_id=project_a.tenant_id)
                payload = context.build_json(tenant_id=project_a.tenant_id)

        self.assertIn("alpha", text)
        self.assertNotIn("beta", text)
        self.assertIn("1 sessions", short)
        self.assertEqual([session["goal"] for session in payload["sessions"]], ["alpha"])

    def test_backfill_assigns_tenant_from_existing_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db"
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text("{}", encoding="utf-8")

            with patch.object(db, "DB_DIR", db_dir), patch.object(db, "DB_PATH", db_dir / "morpheus.db"):
                mission = db.Mission(
                    tab_id="tab-old",
                    goal="old mission",
                    linked_worktree=str(project / "src"),
                )
                db.upsert(mission)

                changed = tenant.backfill_known_tenants()
                updated = db.get("tab-old")
                memory = db.get_memory(mission.mission_id)

        self.assertEqual(changed, 1)
        self.assertEqual(updated.project_root, str(project.resolve()))
        self.assertEqual(memory.project_root, str(project.resolve()))
        self.assertEqual(memory.tenant_id, updated.tenant_id)


if __name__ == "__main__":
    unittest.main()
