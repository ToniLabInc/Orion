"""Knowledge tools: his memory (curated facts, search, notes) and his skills.

Two groups: memory (``memory``, ``memory_search``, ``notes``) and skills (``use_skill``, ``skill_manage``). The tools
are plain functions run on the tools process's worker threads, against the memory system's synchronous API. Where a
request came from decides what it may change: the hub tells the tools process who asked (``current_call()``), the
writer keeps web and app content out of USER.md and MEMORY.md, and skills (instructions he will follow later) change
only from what the user asked directly. The hub sends the vector of a search or a fact along, so this process seldom
needs its own embedding model.
"""

import time
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from orion.base import Query, local_now
from orion.memory.index import Hit, Scope, Span, parse_span
from orion.memory.writer import Op
from orion.skills import expand_paths
from orion.tools import GROUPS, Group, ctx, current_call, publish
from orion.tools.policy import core_memory_allowed

group = Group(GROUPS["memory"])
skills_group = Group(GROUPS["skills"])

SKILL_REFUSAL = "Skills change only from what the user tells you directly; this turn used web or app content."
ACTIONS = {"add": "ADD", "update": "UPDATE", "remove": "DELETE"}


@group.tool(destructive=True, embed_arg="text", title="Curate his memory",
            examples=("Remember that my sister is called Maria", "Forget my old address", "I moved to Athens"))
def memory(
    action: Literal["add", "update", "remove"],
    text: Annotated[str, Field(description='add: ONE fact, as a short sentence saying "the user" (they/them for a '
                                           "pronoun), e.g. \"The user's sister is called Maria.\" update: the whole "
                                           "corrected entry.")] = "",
    target: Literal["user", "orion"] = "user",
    id: Annotated[str, Field(description='update and remove: the entry\'s id from [memory], e.g. "3fa2".')] = "",
    old_text: Annotated[str, Field(description="update and remove without an id: a few words of the entry.")] = "",
    force: Annotated[bool, Field(description="add even though a similar entry exists.")] = False,
) -> str:
    """Curate your long-term memory, shown to you in [memory] with an id for each entry. Add durable facts the moment
    you learn them: target "user" for facts about the user (name, home, family, work, projects, preferences), target
    "orion" for your own notes (this PC, how the user likes things done, lessons learned). Update an entry by id
    when a fact changed; remove one only when the user asks you to forget it. Routines ("when I say X, do Y") are
    skills: save those with skill_manage."""
    call = current_call()
    kind = ACTIONS[action]
    op = Op(kind, target, id=id.strip(), text=text if kind != "DELETE" else "",
            old_text=old_text or (text if kind == "DELETE" and not id else ""), force=force,
            vec=call.qvec if kind == "ADD" else None)
    change = ctx.memory.writer.apply([op], source=call.provenance)[0]
    publish("memory_update", **change.event(call.provenance))
    return change.message


@group.tool(read_only=True, embed_arg="query", title="Search his memory",
            examples=("What did we talk about yesterday?", "Do you remember my router?",
                      "Find my note about the drone"))
def memory_search(
    query: Annotated[str, Field(description="What to look for, in a few words.")],
    scope: Literal["all", "notes", "journal", "conversations", "history"] = "all",
    when: Annotated[str, Field(description='Optional time, e.g. "yesterday", "last week", "in September".')] = "",
) -> str:
    """Search everything you remember beyond [memory]: the journal of past conversations, notes, more facts, and the
    conversations themselves ("history" searches entries that were replaced or removed). Use it for "what did we talk
    about...", "do you remember...", "find my note...", when [recalled] falls short."""
    hits = _search(query, scope, parse_span(when or query, local_now()),
                   include_core=current_call().provenance not in ("brain", "tainted"))
    return "\n".join(f"[{hit.tag()}] {' '.join(hit.text.split())}" for hit in hits) or "Nothing found in memory."


@group.tool(title="Save and find notes",
            examples=("Make a note: drone PID values are 48, 52, 38", "What notes do I have?"))
def notes(
    action: Literal["save", "search", "recent"],
    text: Annotated[str, Field(description="The note (save) or what to look for (search).")] = "",
    title: Annotated[str, Field(description="Optional title of a new note.")] = "",
) -> str:
    """Save a note the user asks you to keep (its own Markdown file), search the notes, or list the recent ones."""
    call = current_call()
    mem = ctx.memory
    if action == "save":
        change = mem.writer.save_note(text, title, source=call.provenance)
        publish("memory_update", **change.event(call.provenance))
        return change.message
    if action == "search":
        hits = _search(text, "notes", None, include_core=False)
        return "\n\n".join(f"[{hit.tag()}]\n{hit.text}" for hit in hits) or "No matching notes."
    lines = [f"- {path.stem} ({time.strftime('%d %b', time.localtime(mtime))})"
             for path, mtime in mem.vault.recent_notes(ctx.config.retrieval.recent_notes)]
    return "\n".join(lines) or "No notes yet."


def _search(text: str, scope: Scope, span: Span | None, *, include_core: bool) -> list[Hit]:
    """Search memory with the request's vector (the hub's, else embedded here), after syncing the index if no Orion
    owns it."""
    vector = current_call().qvec
    mem = ctx.memory
    mem.refresh()
    if vector is None:
        vector = mem.index.embedder.encode_query(text)
    return mem.index.search(Query(text, vector), scope=scope, span=span, k=ctx.config.retrieval.search_results,
                            include_core=include_core)


@skills_group.tool(read_only=True, title="Run a skill", examples=("Good morning", "Movie night"))
def use_skill(name: Annotated[str, Field(description='The skill name, e.g. "morning-briefing".')]) -> str:
    """Load one of your skills (listed in [memory]) and get its step-by-step instructions. Call it first whenever a
    request matches a skill, then follow the instructions. A skill may come with files: read their text with
    skill_file; run its scripts with run_command, which runs them in its sandbox."""
    skill = ctx.skills.get(name)
    if skill is None:
        return f"No skill called '{name}'. Available: {', '.join(ctx.skills.all()) or 'none'}."
    publish("skill_used", name=skill.name)
    if not skill.trusted:
        publish("taint", source=f"The {skill.name} skill")  # its steps are someone else's text
    files = [file for file in ctx.skills.files(skill.name) if file != skill.path.name]
    listed = f"\n\nIt comes with: {', '.join(files)}." if files else ""
    return (f"# Skill: {skill.name}\n{expand_paths(skill, ctx.config.plugins_path)}{listed}\n\n"
            "Follow these steps now.")


@skills_group.tool(read_only=True, title="Read a skill's file")
def skill_file(
    skill: Annotated[str, Field(description='The skill name, e.g. "pdf".')],
    path: Annotated[str, Field(description='The file in the skill\'s folder, e.g. "references/forms.md"; empty: the '
                                           "list of its files.")] = "",
) -> str:
    """Read a text file that comes with one of your skills: its references and assets (no path: the list of its
    files). Its scripts are not read this way: run them with run_command, which runs them in its sandbox."""
    found = ctx.skills.get(skill)
    if found is None:
        raise ToolError(f"No skill called '{skill}'. Available: {', '.join(ctx.skills.all()) or 'none'}.")
    if not path.strip():
        return "\n".join(ctx.skills.files(found.name))
    try:
        text = ctx.skills.read(found.name, path, max_chars=ctx.config.system.read_file_chars, enabled_only=True)
    except (LookupError, ValueError) as e:
        raise ToolError(str(e)) from None
    if not found.trusted:
        publish("taint", source=f"A file of the {found.name} skill")
    return text


@skills_group.tool(destructive=True, title="Create, change or delete a skill",
                   examples=("When I say focus mode, mute the volume and open my notes",))
def skill_manage(
    action: Literal["create", "update", "delete"],
    name: Annotated[str, Field(description='Short kebab-case name, e.g. "movie-night".')],
    description: Annotated[str, Field(description="One sentence: what it does and when to use it (how you will "
                                                  "recognise it later).")] = "",
    instructions: Annotated[str, Field(description="Numbered steps naming the exact tools to call and with which "
                                                   "arguments.")] = "",
    tools: Annotated[str, Field(description='Space-separated tool names it needs, e.g. "volume open media".')] = "",
    triggers: Annotated[str, Field(description='Comma-separated phrases that start it, e.g. "focus mode, time to '
                                               'focus".')] = "",
) -> str:
    """Create, change or delete a skill (a saved routine). Use it ONLY to save what the user teaches you ("when I say
    focus mode, mute the volume and open my notes"); to RUN a skill, call use_skill. The skills that ship with Orion
    and those of plugins can't be changed here."""
    if not core_memory_allowed(current_call().provenance):
        return SKILL_REFUSAL
    try:
        if action == "delete":
            if ctx.skills.delete(name):
                publish("skill_changed", name=name, action="deleted")
                return f"Deleted skill {name}."
            return f"No skill called '{name}'. Available: {', '.join(ctx.skills.all()) or 'none'}."
        if not description.strip() or not instructions.strip():
            return "A skill needs both a description and instructions."
        trigger_list = [t.strip() for t in triggers.split(",") if t.strip()]
        skill = ctx.skills.write(name, description, instructions, tools.split() or None, trigger_list or None)
    except ValueError as e:
        return str(e)
    publish("skill_changed", name=skill.name, action=action)
    spoken = skill.name.replace("-", " ")
    return (f"Skill '{skill.name}' saved but NOT run. Tell the user it is saved and that saying \"{spoken}\" will run "
            "it. Do not claim you performed its steps.")
