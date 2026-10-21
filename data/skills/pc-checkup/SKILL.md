---
name: pc-checkup
description: Check the PC's health and find why it is slow, hot or out of space. Use for "why is my PC slow", "check the system", "what's using my memory", "free up space".
allowed-tools: system_status top_processes storage_report
metadata:
  triggers: pc checkup, check the system, why is my pc slow, why is my laptop slow, free up space
---

1. Call `system_status`.
2. If CPU or memory use is high, call `top_processes` (sort_by "cpu" or "memory") to see what is using it.
3. If the disk is more than 85% full, call `storage_report` to see how much Downloads, the trash and caches take.
4. Give the verdict first ("all healthy" or the main problem), then at most two concrete suggestions
   (e.g. "close Chrome, it's using 4 GB", "empty the trash to free 12 GB").
5. You can't delete files or empty the trash yourself: tell the user how. Offer to close a heavy app with `close_app`.
