---
name: morning-briefing
description: A short daily briefing — weather, today's reminders and tasks, and a couple of headlines. Use for "good morning", "brief me", "what's on today".
allowed-tools: get_weather list_reminders todo get_news
metadata:
  triggers: good morning, brief me, morning briefing, daily briefing, what's on today, what is on today
---

1. Call `get_weather` for the home city (days=1).
2. Call `list_reminders`, and `todo` with action "list".
3. Call `get_news` about the user's interests from memory (top news if none are known).
4. Answer in this order: a greeting that fits the time of day → the weather in one sentence →
   what's scheduled today → open tasks (how many, and the first two) → two headlines.

Keep it under 60 spoken words. Skip anything that's empty; say "your schedule is clear" instead of listing nothing.
