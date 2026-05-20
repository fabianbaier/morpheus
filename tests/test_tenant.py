import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import context, db, ledger, tenant


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

    def test_db_filters_loops_by_project_tenant(self) -> None:
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
                db.create_loop(
                    "project a loop",
                    "prompt",
                    60,
                    "codex exec",
                    tenant_id=project_a.tenant_id,
                    project_root=project_a.root_path,
                )
                db.create_loop(
                    "project b loop",
                    "prompt",
                    60,
                    "codex exec",
                    tenant_id=project_b.tenant_id,
                    project_root=project_b.root_path,
                )

                loops_a = db.all_loops(tenant_id=project_a.tenant_id)

        self.assertEqual([loop.name for loop in loops_a], ["project a loop"])

    def test_db_backfills_legacy_loop_project_from_target_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db"
            project = db.ProjectTenant(
                tenant_id=tenant.tenant_id_for_root(root / "a"),
                name="a",
                root_path=str(root / "a"),
                root_kind="cwd",
            )

            with patch.object(db, "DB_DIR", db_dir), patch.object(db, "DB_PATH", db_dir / "morpheus.db"):
                project = db.upsert_project_tenant(project)
                db.upsert_memory(db.MissionMemory(
                    mission_id="m_target",
                    tenant_id=project.tenant_id,
                    project_root=project.root_path,
                    title="target mission",
                ))
                legacy = db.create_loop(
                    "legacy loop",
                    "prompt",
                    60,
                    "codex exec",
                    target_mission_id="m_target",
                )

                loops = db.all_loops(tenant_id=project.tenant_id)
                refreshed = db.get_loop(legacy.id)

        self.assertEqual([loop.id for loop in loops], [legacy.id])
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.tenant_id, project.tenant_id)
        self.assertEqual(refreshed.project_root, project.root_path)

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

    def test_prune_empty_project_tenants_keeps_nonempty_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db"
            with patch.object(db, "DB_DIR", db_dir), patch.object(db, "DB_PATH", db_dir / "morpheus.db"):
                empty = db.upsert_project_tenant(
                    db.ProjectTenant(
                        tenant_id=tenant.tenant_id_for_root(root / "empty"),
                        name="empty",
                        root_path=str(root / "empty"),
                    )
                )
                used = db.upsert_project_tenant(
                    db.ProjectTenant(
                        tenant_id=tenant.tenant_id_for_root(root / "used"),
                        name="used",
                        root_path=str(root / "used"),
                    )
                )
                db.upsert(
                    db.Mission(
                        tab_id="tab-used",
                        tenant_id=used.tenant_id,
                        project_root=used.root_path,
                        goal="used project",
                    )
                )

                candidates = db.empty_project_tenants()
                results = db.prune_empty_project_tenants()

                self.assertEqual([item.tenant_id for item in candidates], [empty.tenant_id])
                self.assertEqual([item.tenant_id for item in results], [empty.tenant_id])
                self.assertIsNone(db.get_project_tenant(empty.tenant_id))
                self.assertIsNotNone(db.get_project_tenant(used.tenant_id))

    def test_delete_project_tenant_purges_related_db_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_dir = root / "db"
            with patch.object(db, "DB_DIR", db_dir), patch.object(db, "DB_PATH", db_dir / "morpheus.db"):
                project = db.upsert_project_tenant(
                    db.ProjectTenant(
                        tenant_id=tenant.tenant_id_for_root(root / "project"),
                        name="project",
                        root_path=str(root / "project"),
                    )
                )
                other = db.upsert_project_tenant(
                    db.ProjectTenant(
                        tenant_id=tenant.tenant_id_for_root(root / "other"),
                        name="other",
                        root_path=str(root / "other"),
                    )
                )
                mission = db.Mission(
                    tab_id="tab-a",
                    session_id="session-a",
                    tenant_id=project.tenant_id,
                    project_root=project.root_path,
                    goal="delete me",
                )
                other_mission = db.Mission(
                    tab_id="tab-b",
                    tenant_id=other.tenant_id,
                    project_root=other.root_path,
                    goal="keep me",
                )
                db.upsert(mission)
                db.upsert(other_mission)
                db.add_event(mission.mission_id, "proof", "event to delete")
                db.add_artifact(mission.mission_id, "test", "artifact.txt", "pass", "artifact to delete")
                db.add_edge(mission.mission_id, other_mission.mission_id, "related")
                db.add_note("note to delete", tab_id=mission.tab_id, session_id=mission.session_id)
                loop = db.create_loop(
                    "loop to delete",
                    "prompt",
                    60,
                    "codex exec",
                    target_mission_id=mission.mission_id,
                    target_tab_id=mission.tab_id,
                )
                db.record_loop_run(
                    loop.id,
                    started_at=1,
                    finished_at=2,
                    status="ok",
                    exit_code=0,
                    output_path="",
                    summary="run to delete",
                    target_mission_id=mission.mission_id,
                    target_tab_id=mission.tab_id,
                )
                ledger.log_action(
                    "spawn",
                    tab_id=mission.tab_id,
                    details={"mission_id": mission.mission_id, "tenant_id": project.tenant_id},
                )

                result = db.delete_project_tenant(project.tenant_id, allow_live=True)

                self.assertEqual(result.blocked_reason, "")
                self.assertIsNone(db.get_project_tenant(project.tenant_id))
                self.assertIsNone(db.get(mission.tab_id))
                self.assertIsNone(db.get_memory(mission.mission_id))
                self.assertEqual(db.recent_events(mission.mission_id), [])
                self.assertEqual(db.artifacts_for_mission(mission.mission_id), [])
                self.assertEqual(db.edges_for_id(mission.mission_id), [])
                self.assertEqual(db.notes_for_tab(mission.tab_id), [])
                self.assertEqual(db.all_loops(), [])
                self.assertEqual(ledger.recent_actions(), [])
                self.assertIsNotNone(db.get_project_tenant(other.tenant_id))
                self.assertIsNotNone(db.get(other_mission.tab_id))


if __name__ == "__main__":
    unittest.main()
