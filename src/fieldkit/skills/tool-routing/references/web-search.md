# Web search routes

Use `tvly` for ordinary web search, extraction, crawl, map, and research:

```bash
tvly search "<query>" --json
tvly extract <url> --json
tvly crawl <url> --json
tvly map <url> --json
tvly research run "<question>" --json
```

Use `tvly <command> --help` before unfamiliar flags.

The direct global `brave_search` MCP is an exception for Brave's independent
index. Use it only when a second index is materially useful after Tavily, and
call `brave_search__brave_web_search` or `brave_search__brave_llm_context`. It is
not a gateway group or a fallback for a missing Tavily CLI credential.
