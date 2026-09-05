"""Shared Markdown assembly for Hermes context and S2S instructions."""

from collections.abc import Iterable


MAX_CONTEXT_CHARS = 20_000


def compose_context_document(
    base: str, prompt_sections: Iterable[tuple[str, str]]
) -> str:
    """Preserve the Hermes sync format and catalog section order."""

    sections = [base.rstrip()]
    for title, content in prompt_sections:
        title = title.strip()
        content_lines = content.strip().splitlines()
        if (
            content_lines
            and content_lines[0].startswith("# ")
            and content_lines[0][2:].strip().casefold() == title.casefold()
        ):
            content_lines = content_lines[1:]
        content = "\n".join(
            f"#{line}" if line.startswith("#") else line for line in content_lines
        ).strip()
        sections.append(f"## {title}\n\n{content}")

    rendered = "\n\n".join(sections) + "\n"
    if len(rendered) > MAX_CONTEXT_CHARS:
        raise ValueError("generated HERMES.md exceeds the 20,000 character limit")
    return rendered
