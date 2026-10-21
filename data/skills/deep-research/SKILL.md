---
name: deep-research
description: Research a question properly on the web and give a sourced answer. Use when the user asks to research, look into or compare something, or needs an accurate up-to-date answer.
allowed-tools: web_search read_webpage notes
metadata:
  triggers: research, look into
---

1. Turn the question into one to three focused search queries.
2. For each query call `web_search`, then pick the two or three most authoritative results
   (official documentation, reputable outlets).
3. Call `read_webpage` on those pages — never answer from search snippets alone.
4. Compare the sources and notice where they disagree.
5. Answer with the key finding first, then the supporting points and which source said them.
   Spoken: three or four sentences, then offer to save the details. On screen: short sections with links.
6. If the user wants to keep it, call `notes` with action "save" and the question as the title, including the sources.
