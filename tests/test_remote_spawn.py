import unittest

from morpheus import remote, tenant


class RemoteSpawnCommandTest(unittest.TestCase):
    def test_codex_spawn_receives_initial_prompt(self) -> None:
        command = tenant.command_with_prompt_in_project(
            "codex",
            "/tmp/morpheus project",
            "list this directory",
        )

        self.assertEqual(
            command,
            "cd '/tmp/morpheus project' && codex 'list this directory'",
        )

    def test_prompt_placeholder_supports_custom_commands(self) -> None:
        command = tenant.command_with_prompt_in_project(
            "codex --model gpt-5.5 {prompt}",
            "/tmp/morpheus",
            "what's here?",
        )

        self.assertEqual(
            command,
            """cd /tmp/morpheus && codex --model gpt-5.5 'what'"'"'s here?'""",
        )

    def test_non_prompt_aware_command_is_left_alone(self) -> None:
        command = tenant.command_with_prompt_in_project("top", "/tmp/morpheus", "ignored")

        self.assertEqual(command, "cd /tmp/morpheus && top")

    def test_clean_terminal_output_filters_codex_startup_noise(self) -> None:
        cleaned = remote.clean_terminal_output(
            "Last login: Tue Jun  9 21:45:45 on ttys117\n"
            "cd /Users/fabianbaier/github/fabianbaier/morpheus && codex 'list'\n"
            "fabianbaier@Fabians-MacBook-Pro ~ % cd /Users/fabianbaier/github/fabianbaier/morpheus && codex 'list'\n"
            "\x1b[32mOpenAI Codex\x1b[0m\n"
            "│ https://github.com/openai/codex/releases/latest │\n"
            "╰────────────────────────────────────────────────╯\n"
            "model: gpt-5.5\n"
            "> G2: list files\n"
            "•Working(1s • esc to interrupt)\n"
            "Here is the current directory tree:\n"
            "README.md\n"
            "────────────────────\n"
            "›Explain this codebase\n"
            "gpt-5.5 xhigh · ~/github/fabianbaier/morpheus\n"
            "plugins/\n",
            line_limit=4,
        )

        self.assertEqual(
            cleaned["text"],
            "Here is the current directory tree:\nREADME.md\nplugins/",
        )


if __name__ == "__main__":
    unittest.main()
