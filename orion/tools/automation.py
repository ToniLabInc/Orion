"""His scheduled jobs and the machine's watchers, as tools: served inside Orion's own process, next to his controls,
because they act on the running scheduler (``orion.jobs``) and the sensors (``orion.watchers``).

The HUD's REST endpoints go through the same services, so a job made by voice and one made on the Automations page
are the same thing.
"""

import json
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import Field

from orion.base import GroupInfo
from orion.jobs import ScheduleError
from orion.tools import Group

if TYPE_CHECKING:
    from orion.runtime import Runtime

JOBS: Final = GroupInfo("jobs", "Scheduled jobs", "Requests he runs on his own at set times (a morning briefing, a "
                                                  "check every hour), with their results spoken, shown or sent.")

SCHEDULE_HELP: Final = ("A cron schedule in this machine's time zone: minute hour day-of-month month day-of-week. "
                        "Examples: '30 7 * * 1-5' weekdays at 7:30; '0 9 * * *' every day at 9:00; '0 */2 * * *' every "
                        "two hours; '0 18 * * 5' Fridays at 18:00; '15 8 1 * *' the 1st of each month at 8:15.")
WATCHERS: Final = GroupInfo("watchers", "Watchers", "The machine's battery, temperatures, disk and memory, and the "
                                                    "warnings he gives when one needs the user.")
Deliver = list[Literal["speak", "hud", "discord"]]


def groups(rt: "Runtime") -> list[Group]:
    """The jobs tools, bound to this running Orion.

    Args:
        rt: the running Orion (its jobs and their runner).

    Returns:
        The group.
    """
    jobs = Group(JOBS)

    def line(job: dict[str, object]) -> str:
        """One job in words for the model."""
        state = "on" if job["enabled"] else "off"
        last = f"; last run {job['last_status'] or 'never'}" if job["last_run"] else "; never ran yet"
        return f"#{job['id']} {job['name']}: {job['explain']} ({job['schedule']}), {state}{last}. " \
               f"Request: {job['prompt']}"

    @jobs.tool(title="Schedule a job", examples=("Every weekday at 7:30 give me a briefing",
                                                 "Every hour tell me if the build is red"))
    def job_create(name: Annotated[str, Field(description="A short name, e.g. 'Morning briefing'.")],
                   schedule: Annotated[str, Field(description=SCHEDULE_HELP)],
                   prompt: Annotated[str, Field(description="What to do each time, as the user would ask it.")],
                   deliver: Annotated[Deliver | None, Field(
                       description="Where the result goes: speak (aloud), hud (shown), discord (sent). Default: "
                                   "the settings' choice.")] = None) -> str:
        """Schedule a request to run on its own at set times (a briefing, a check, a reminder that needs the web).
        Read the schedule's meaning back to the user."""
        try:
            job = rt.jobs.create(name, schedule, prompt, deliver)
        except (ValueError, ScheduleError) as e:
            return f"Error: {e}"
        return f"Scheduled {line(job)}"

    @jobs.tool(read_only=True, title="List the scheduled jobs", examples=("What jobs do I have?",))
    def job_list() -> str:
        """List the scheduled jobs with their schedules and last results."""
        found = rt.jobs.all()
        return "\n".join(line(job) for job in found) if found else "There are no scheduled jobs."

    @jobs.tool(title="Change a scheduled job", examples=("Move the briefing to 8", "Pause the build check"))
    def job_update(job: Annotated[str, Field(description="Its number ('#2') or name.")],
                   name: str | None = None,
                   schedule: Annotated[str | None, Field(description=SCHEDULE_HELP)] = None,
                   prompt: str | None = None,
                   enabled: Annotated[bool | None, Field(description="False pauses it, true resumes it.")] = None,
                   deliver: Deliver | None = None) -> str:
        """Change a scheduled job: its name, schedule, request, where its result goes, or pause/resume it."""
        try:
            changed = rt.jobs.update(job, name=name, schedule=schedule, prompt=prompt, deliver=deliver,
                                     enabled=enabled)
        except (LookupError, ValueError, ScheduleError) as e:
            return f"Error: {e}"
        return f"Changed {line(changed)}"

    @jobs.tool(destructive=True, title="Delete a scheduled job", examples=("Delete the build check",))
    def job_delete(job: Annotated[str, Field(description="Its number ('#2') or name.")]) -> str:
        """Delete a scheduled job."""
        try:
            return f"Deleted the job {rt.jobs.delete(job)}."
        except LookupError as e:
            return f"Error: {e}"

    @jobs.tool(title="Run a scheduled job now", examples=("Run the briefing now",))
    async def job_run(job: Annotated[str, Field(description="Its number ('#2') or name.")]) -> str:
        """Run a scheduled job now, in the background; its result is delivered as usual."""
        try:
            found = rt.jobs.find(job)
        except LookupError as e:
            return f"Error: {e}"
        rt.run_job(found)
        return f"Running {found['name']} now; its result comes as usual ({json.dumps(found['deliver'])})."

    watch = Group(WATCHERS)

    @watch.tool(read_only=True, title="Read the machine's sensors and warnings",
                examples=("How's the battery?", "Is anything wrong with the PC?", "What are you watching?"))
    def watchers_status() -> str:
        """The machine's sensors now (battery, temperatures, disk, memory), what each watcher warns about and at
        which limit, and the warnings on now. Limits change with the settings tools (the watchers section)."""
        lines = []
        for rule in rt.watchers.status():
            if not rule["available"]:
                lines.append(f"- {rule['title']}: this machine has no such reading")
                continue
            limit = f"warns {'above' if rule['above'] else 'below'} {rule['limit']:g}{rule['unit']}" \
                if rule["enabled"] else "off"
            value = "no reading now" if rule["value"] is None else f"{rule['value']:.0f}{rule['unit']}"
            lines.append(f"- {rule['title']}: {value}; {limit}{' — WARNING ON' if rule['alert'] else ''}")
        return "\n".join(lines)

    return [jobs, watch]
