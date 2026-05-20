import unittest

from morpheus import core, iterm_client, naming


def _tab(
    *,
    tab_id: str = "tab-123",
    session_id: str = "session-123",
    current_name: str = "Python",
    buffer: str = "",
) -> iterm_client.TabInfo:
    return iterm_client.TabInfo(
        tab_id=tab_id,
        session_id=session_id,
        window_id="window-123",
        buffer=buffer,
        current_name=current_name,
    )


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
                "j k n new d kill p prune s snapshot / note r refresh q quit\n"
            ),
        )

        self.assertTrue(core._should_ignore_tab(tab))

    def test_do_not_ignore_regular_morpheus_project_output(self) -> None:
        tab = _tab(
            current_name="codex",
            buffer="Editing the Morpheus PRD. Need to mention mission control more clearly.",
        )

        self.assertFalse(core._should_ignore_tab(tab))


if __name__ == "__main__":
    unittest.main()
