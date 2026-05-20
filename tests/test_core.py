import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morpheus import core, db, iterm_client, naming


def _tab(
    *,
    tab_id: str = "tab-123",
    session_id: str = "session-123",
    current_name: str = "Python",
    buffer: str = "",
    cwd: str = "",
) -> iterm_client.TabInfo:
    return iterm_client.TabInfo(
        tab_id=tab_id,
        session_id=session_id,
        window_id="window-123",
        buffer=buffer,
        current_name=current_name,
        cwd=cwd,
    )


class FakeLogger:
    def debug(self, *args, **kwargs) -> None:
        pass

    def info(self, *args, **kwargs) -> None:
        pass

    def exception(self, *args, **kwargs) -> None:
        pass


class CoreTest(unittest.TestCase):
    def test_ignore_tab_by_explicit_dashboard_identity(self) -> None:
        tab = _tab(tab_id="self-tab", session_id="self-session")

        self.assertTrue(
            core._should_ignore_tab(
                tab,
                ignored_tab_ids={"self-tab"},
                ignored_session_ids=set(),
            )
        )
        self.assertTrue(
            core._should_ignore_tab(
                tab,
                ignored_tab_ids=set(),
                ignored_session_ids={"self-session"},
            )
        )

    def test_ignore_tab_by_morpheus_title(self) -> None:
        tab = _tab(current_name=naming.MORPHEUS_TAB_PREFIX)

        self.assertTrue(core._should_ignore_tab(tab))

    def test_ignore_dashboard_buffer_when_title_rename_fails(self) -> None:
        tab = _tab(
            current_name='Python"',
            buffer=(
                "MORPHEUS\n"
                "mission control v0.7.0a5 - follow the white rabbit\n"
                "MISSION CARD\n"
                "j k n new d kill p prune s snapshot / note r resume q quit\n"
            ),
        )

        self.assertTrue(core._should_ignore_tab(tab))

    def test_do_not_ignore_regular_morpheus_project_output(self) -> None:
        tab = _tab(
            current_name="codex",
            buffer="Editing the Morpheus PRD. Need to mention mission control more clearly.",
        )

        self.assertFalse(core._should_ignore_tab(tab))


class CoreTickTest(unittest.IsolatedAsyncioTestCase):
    async def test_tick_persists_codex_resume_id_from_live_buffer(self) -> None:
        resume_id = "019e466d-0fd8-7441-aa1f-32a5db211a73"
        tab = _tab(
            tab_id="tab-codex",
            session_id="session-codex",
            current_name="codex",
            cwd="/tmp/work",
            buffer=(
                "Token usage: total=5,950 input=5,936 output=14\n"
                f"To continue this session, run codex resume {resume_id}"
            ),
        )

        async def fake_enumerate_tabs(connection):
            return [tab]

        async def fake_set_tab_name(connection, session_id, name):
            return True

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db, "DB_DIR", root),
                patch.object(db, "DB_PATH", root / "morpheus.db"),
                patch.object(core.iterm_client, "enumerate_tabs", new=fake_enumerate_tabs),
                patch.object(core.iterm_client, "set_tab_name", new=fake_set_tab_name),
                patch.object(
                    core.cfg_mod,
                    "load",
                    new=lambda: {
                        "token_guard": {"enabled": False},
                        "worktree": {"warn_on_collision": False},
                    },
                ),
                patch.object(core.ctx_mod, "write_context_file", new=lambda: None),
                patch.object(core.ctx_mod, "write_context_json", new=lambda: None),
                patch.object(core.daemon_mod, "write_beacon", new=lambda: None),
            ):
                mission = db.Mission(
                    tab_id=tab.tab_id,
                    session_id=tab.session_id,
                    goal="Codex mission",
                    state="working",
                    cmd="codex --yolo",
                )
                db.upsert(mission)

                count = await core._tick(object(), FakeLogger())
                live = db.get(mission.tab_id)
                memory = db.get_memory(mission.mission_id)

        self.assertEqual(count, 1)
        expected_root = str(Path("/tmp/work").resolve())
        self.assertEqual(live.project_root, expected_root)
        self.assertTrue(live.tenant_id.startswith("p_"))
        self.assertEqual(memory.project_root, expected_root)
        self.assertEqual(memory.tenant_id, live.tenant_id)
        self.assertEqual(memory.resume_ref, resume_id)
        self.assertEqual(memory.resume_confidence, "exact")
        self.assertIn(f"cd /tmp/work && codex --yolo resume {resume_id}", memory.resume_command)


if __name__ == "__main__":
    unittest.main()
