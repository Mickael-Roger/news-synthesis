# Project-Specific Agent Guidelines

## Overview
- Purpose: Python job for news synthesis from podcasts, YouTube videos, and RSS feeds
- Language: Python
- Execution: Runs via command-line with --daily or --weekly flags

## Dependencies
- PyYAML - Configuration file handling
- assemblyai - Podcast transcription
- litellm - LLM usage
- markdown - Markdown to HTML conversion
- requests - HTTP requests for YouTube transcripts via fetchtranscript.com
- smtplib - Email sending (built-in)
- email.mime.text, email.mime.multipart - Email message creation (built-in)
- sqlite3 - Database (built-in)

## Rules
- Use English for all code, comments, variable names, and documentation
- Follow PEP 8 style guidelines
- Keep the job lightweight and efficient

## Logging System
The project uses Python's built-in logging module with a standardized format:
- Format: `YYYY-MM-DD HH:MM:SS [LEVEL] module_name - message`
- Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
- Default level: INFO

### Log Level Configuration
Use `--log-level` command-line argument to set verbosity:
- `DEBUG`: Detailed information for diagnosing problems
- `INFO` (default): Confirmation that things are working as expected
- `WARNING`: Indication that something unexpected happened
- `ERROR`: Serious problem occurred
- `CRITICAL`: Serious error indicating the program may not be able to continue

### Error Logging
All failures are logged with appropriate levels:
- YouTube transcript fetch failures (video ID extraction, API errors, timeouts)
- Podcast transcription failures (AssemblyAI errors, network issues)
- LLM synthesis failures (API errors, timeouts)
- Database operation failures
- Email sending failures (SMTP errors)
- File I/O failures

## Configuration Structure (config.yaml)
- `assemblyai.api_key`: API key for podcast transcription
- `llm`: LLM configuration (endpoint, model_name, small_model_name, api_key)
- `smtp`: Email configuration (server, port, ssl, username, password, sender_email, destination_email)
- `fetchtranscript.api_key`: API key for YouTube transcript fetching via fetchtranscript.com
- `data`: Data paths (directory, sqlite_path)
- `feed_categories.youtube_category`: List of category names for YouTube content
- `feed_categories.podcast_category`: List of category names for podcast content
- `ignored_feeds`: Optional list of feed names to skip entirely (no synthesis performed)

## Command-Line Arguments
- `--config`: (Required) Path to config.yaml file
- `--daily`: Process last 24 hours of news (default)
- `--weekly`: Process last 7 days of news
- `--no-send`: Skip email sending at the end of the process
- `--log-level`: Set logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) - default: INFO

## Pipeline Status
- **Regular news**: Fetched from DB → LLM synthesis (with `fetch_article_content` tool available — if the LLM finds content empty/insufficient, it invokes the tool to fetch the article HTML, which is sent back as a tool response for the LLM to complete the synthesis) → written to `{data.directory}/news/{id}.md` → synthesis converted to HTML and stored in `entry.content` in SQLite
- **YouTube news**: Fetched from DB → `fetchtranscript.com` API transcript fetch (GET `/v1/transcripts/{video_id}?format=text`) → raw transcript written to `{data.directory}/youtube/{id}.txt` → LLM synthesis → written to `{data.directory}/news/{id}.md` → synthesis converted to HTML and stored in `entry.content` in SQLite
- **Podcast news**: Fetched from DB → AssemblyAI transcription (direct URL) → raw transcript written to `{data.directory}/podcasts/{id}.txt` → LLM synthesis → written to `{data.directory}/news/{id}.md` → synthesis converted to HTML and stored in `entry.content` in SQLite
- **Global synthesis**: Check if synthesis file exists at `{data.directory}/synthesis/YYYY-MM-DD.md` (daily) or `{data.directory}/synthesis/YYYY-Week_XX.md` (weekly) → if exists, read existing file; if not exists, merge all `{data.directory}/news/{id}.md` files for the period and send to LLM → structured Markdown digest grouped by category/theme → written to file → (unless `--no-send` flag is set) markdown content converted to bilingual HTML (English + French) via small LLM model → sent via email to configured recipient

## Key Functions
- `fetch_article_content(url)`: Fetches the full article HTML from a URL using `requests` with a browser-like User-Agent. Used as a fallback when the LLM cannot access the RSS content (e.g., some news providers block LLM platform IP ranges). Returns the raw HTML content or an error message string on failure. Timeout: 30 s.
- `FETCH_ARTICLE_TOOL`: Tool definition (OpenAI function-calling format) for `fetch_article_content`, passed to the LLM in `synthesize_news` so the LLM can request article fetching when content is empty or insufficient.
- `synthesize_news(item, config)`: Calls LiteLLM with category, feed, title, URL and content; provides the `FETCH_ARTICLE_TOOL` so the LLM can request article fetching when RSS content is empty/truncated/insufficient. If the LLM invokes the tool, the article is fetched via `fetch_article_content`, the result is sent back as a tool response, and the LLM completes the synthesis. Instructs the LLM to produce a **two-part synthesis** using bullet points throughout (no prose paragraphs): a TL;DR (`## Summary`, 2–3 bullets covering what/who/why) followed by a detailed breakdown (`## Detailed Synthesis`) as bullet points covering key facts, context, data/statistics, quotes, implications, dates/locations/entities, technical details; always responds in English regardless of the source language; explicitly instructs the LLM **not to fabricate content**; returns synthesis text
- `convert_news_synthesis_to_html(synthesis_markdown, config)`: Converts a per-item Markdown synthesis to a styled HTML fragment using the small LLM model (`llm.small_model_name`). Produces an HTML body fragment (no `<html>`/`<head>`/`<body>` tags) with inline CSS, preserving links. Falls back to the `markdown` library if the LLM call fails. Strips any markdown code fences from the LLM output.
- `update_db_content(item_id, synthesis_markdown, config)`: Converts synthesis Markdown to HTML via `convert_news_synthesis_to_html` (small LLM with fallback to `markdown` library) and writes it to `entry.content` in the SQLite DB; the `.md` file on disk is left unchanged
- `process_regular_news(item, config)`: Creates `{data.directory}/news/` dir if needed, writes synthesis to `{id}.md`, then calls `update_db_content`
- `get_youtube_video_id(url)`: Extracts the YouTube video ID from a URL (supports `youtube.com/watch?v=`, `youtu.be/`, and `m.youtube.com` formats)
- `get_youtube_transcript(item, config)`: Downloads transcript via `fetchtranscript.com` API (uses `config["fetchtranscript"]["api_key"]`), uses GET request to `/v1/transcripts/{video_id}?format=text`, writes plain text to `{data.directory}/youtube/{id}.txt`; skips if file already exists; returns path or `None`
- `process_youtube_news(item, config)`: Entry point for YouTube items; skips if synthesis (`{data.directory}/news/{id}.md`) already exists; otherwise calls `get_youtube_transcript` (which reuses cached `.txt` if present), synthesizes the transcript via LLM, writes to `{data.directory}/news/{id}.md`, and calls `update_db_content`. The transcript and synthesis are tracked independently — a cached transcript does **not** suppress synthesis if the `.md` file is missing. Logs "cached" vs "fetched" at INFO level so the transcript source is always visible. Also guards against empty transcript files before calling the LLM.
- `_transcribe_via_local_download(audio_url, item_id, transcriber, config)`: Fallback called by `get_podcast_transcript` when AssemblyAI returns a "Download error". Downloads the audio file locally via `requests` (streaming, 120 s timeout), checks the `Content-Type` is audio/video, saves to a `tempfile`, passes the local path to AssemblyAI (the SDK uploads it automatically), then deletes the temp file. Returns the AssemblyAI Transcript object or `None` on failure.
- `get_podcast_transcript(item, config)`: Extracts the MP3 audio URL from `item["attributes"]` (JSON field with an `enclosures` array, e.g. `{"enclosures":[{"url":"...","type":"audio/mpeg",...}]}`); falls back to `item["link"]` only if no enclosure URL is found. Passes the audio URL directly to AssemblyAI for transcription; if AssemblyAI returns a "Download error", falls back to `_transcribe_via_local_download`. Writes plain text to `{data.directory}/podcasts/{id}.txt`; skips if file already exists; returns path or `None`
- `process_podcast_news(item, config)`: Entry point for podcast items; calls `get_podcast_transcript`, then synthesizes via LLM and writes to `{data.directory}/news/{id}.md`; skips if synthesis already exists; calls `update_db_content` after writing
- `collect_period_syntheses(news_items, config)`: Reads all existing `{data.directory}/news/{id}.md` files for the period's news items (skipping ignored feeds and missing files); returns a list of dicts with synthesis text and metadata
- `make_global_synthesis(syntheses, frequency, config)`: Sends all collected syntheses to the LLM with instructions to classify each item as either **TYPE A — Current News** (recent events, announcements, launches) or **TYPE B — Tech Articles** (evergreen deep dives, tutorials, opinion pieces). Produces a structured Markdown digest with three top-level sections: `## Highlights` (2–3 bullet points for the most important news of the period), `## News` (TYPE A items grouped under dynamic topic subsections — e.g. AI, Cloud, Cybersecurity — each entry max 2–3 bullets, same story from multiple sources merged into one entry, title as Markdown link), and `## Tech Articles & Deep Dives` (TYPE B items as single bullet points with Markdown link, **feed name in bold**, and one-sentence description). Returns the synthesis string or `None`.
- `write_global_synthesis(synthesis_text, frequency, config)`: Creates `{data.directory}/synthesis/` dir if needed; writes the global synthesis to `YYYY-MM-DD.md` (daily) or `YYYY-Week_XX.md` (weekly, using ISO year and week number); skips if file already exists; returns the file path
- `convert_markdown_to_html_via_llm(markdown_content, subject, config)`: Uses the small LLM model (`llm.small_model_name`) to convert the global markdown digest into a beautiful, modern HTML email containing both an English version and a full French translation, displayed sequentially with a visual separator. Strips any markdown code fences from the LLM output. Returns the HTML string or `None` on failure.
- `_fallback_markdown_to_html(content, subject)`: Fallback HTML conversion using the `markdown` library (no LLM). Used when `convert_markdown_to_html_via_llm` fails. Produces a basic HTML document with inline CSS.
- `send_email(subject, content, config)`: Sends an email using SMTP configuration from config.yaml; converts markdown content to bilingual HTML via the small LLM model (with fallback to basic `markdown` library conversion); sends both plain text and HTML versions; uses SMTP_SSL if ssl=true, otherwise uses SMTP with STARTTLS; returns True on success, False on failure
- LiteLLM is called with `openai/{model_name}` prefix to route to the configured OpenAI-compatible endpoint

## Important
- **Update this AGENTS.md file after any relevant changes** (new features, bug fixes, configuration changes)

## Lessons Learned
- **AssemblyAI speech model**: The AssemblyAI API no longer accepts the generic `"universal"` model name. Use `speech_models=["universal-3-pro", "universal-2"]` with `language_detection=True` in `TranscriptionConfig`. The SDK's `SpeechModel.universal` enum resolves to `"universal"` which is now rejected by the API.
- **AssemblyAI URL transcription**: No need to download podcast audio locally first. AssemblyAI can transcribe directly from a public URL — pass the audio URL straight to `transcriber.transcribe()`.
- **Podcast audio URL**: The `entry.link` field in FreshRSS is the webpage URL for the podcast episode, **not** the MP3. The actual audio URL is stored in `entry.attributes` as JSON: `{"enclosures":[{"url":"https://...mp3","type":"audio/mpeg",...}]}`. Always extract the URL from `enclosures[0].url`; fall back to `item["link"]` only as a last resort. The `attributes` column must be included in the SQL SELECT query.
- **Logging system**: The project uses Python's built-in logging module with standardized format `YYYY-MM-DD HH:MM:SS [LEVEL] module_name - message`. All failures are logged with appropriate levels (ERROR for critical failures, WARNING for non-critical issues). Use `--log-level` argument to control verbosity.
- **YouTube transcript vs synthesis independence**: The transcript cache (`data/youtube/ID.txt`) and the synthesis file (`data/news/ID.md`) are independent. A cached transcript must not suppress synthesis — only the existence of the `.md` file should skip synthesis. `process_youtube_news` checks `synthesis_path` first; `get_youtube_transcript` is called afterwards and simply returns the cached path if the `.txt` exists. Also guard against empty transcript files (`.strip()` check) before sending to the LLM.
- **AssemblyAI local download fallback**: Some podcast CDNs (e.g. Simplecast) block AssemblyAI's download agent, resulting in a `"Download error, unable to download ..."` transcript error. When this error is detected in `get_podcast_transcript`, the code falls back to `_transcribe_via_local_download`: it downloads the MP3 locally via `requests` (streaming, 120 s timeout), verifies the `Content-Type` is audio/video, saves to a `tempfile`, passes the local path to AssemblyAI (SDK uploads automatically), then removes the temp file. The `transcriber` object is reused so the same `TranscriptionConfig` applies.
