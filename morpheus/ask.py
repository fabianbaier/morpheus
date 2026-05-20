"""`morpheus ask "<query>"` — conversational query over current state.

Builds a state snapshot via brief.gather_state(), prepends the user's
question + a system prompt, pipes through `claude -p` (or `codex exec`)
and prints the answer.
"""

from __future__ import annotations

from typing import Optional

from morpheus import brief, ledger

# Rough cost model: charge a few cents per ask.
ASK_COST_DOLLARS = 0.03


def ask(query: str, use_llm: bool = True,
        gh_repos: Optional[list[str]] = None) -> str:
    """Answer a question about the current morpheus state.

    Returns markdown text. Falls back to the raw state snapshot if no
    LLM is available.
    """
    state = brief.gather_state(gh_repos=gh_repos, include_gh=True)
    snapshot = brief.build_template_brief(state)

    if not use_llm:
        return (
            f"## Question\n\n> {query}\n\n"
            f"## (No LLM — raw state)\n\n{snapshot}"
        )

    prompt = (
        "You are Morpheus, the mission control for a solo developer's "
        "agent sessions. Answer the user's question using ONLY the state "
        "snapshot below. Be concise (≤12 lines). If the answer requires "
        "info the snapshot doesn't have, say so explicitly. If the user is "
        "asking for an action (kill X, snapshot Y), tell them the exact "
        "morpheus command to run.\n\n"
        f"QUESTION: {query}\n\n"
        f"CURRENT STATE:\n\n{snapshot}"
    )
    answer = brief._run_claude(prompt)
    if answer is None:
        answer = brief._run_codex(prompt)
    if answer is None:
        return (
            f"## Question\n\n> {query}\n\n"
            f"## (neither claude nor codex available — raw state)\n\n{snapshot}"
        )

    # Log the cost.
    ledger.log_cost(
        kind="ask",
        description=query[:120],
        tokens=len(prompt) // 4 + len(answer) // 4,
        dollars=ASK_COST_DOLLARS,
    )
    return f"## Question\n\n> {query}\n\n## Morpheus says\n\n{answer}\n"
