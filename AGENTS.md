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
- `llm`: LLM configuration (endpoint, model_name, api_key)
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
- **Regular news**: Fetched from DB → LLM synthesis → written to `{data.directory}/news/{id}.md` → synthesis converted to HTML and stored in `entry.content` in SQLite
- **YouTube news**: Fetched from DB → `fetchtranscript.com` API transcript fetch (GET `/v1/transcripts/{video_id}?format=text`) → raw transcript written to `{data.directory}/youtube/{id}.txt` → LLM synthesis → written to `{data.directory}/news/{id}.md` → synthesis converted to HTML and stored in `entry.content` in SQLite
- **Podcast news**: Fetched from DB → AssemblyAI transcription (direct URL) → raw transcript written to `{data.directory}/podcasts/{id}.txt` → LLM synthesis → written to `{data.directory}/news/{id}.md` → synthesis converted to HTML and stored in `entry.content` in SQLite
- **Global synthesis**: Check if synthesis file exists at `{data.directory}/synthesis/YYYY-MM-DD.md` (daily) or `{data.directory}/synthesis/YYYY-Week_XX.md` (weekly) → if exists, read existing file; if not exists, merge all `{data.directory}/news/{id}.md` files for the period and send to LLM → structured Markdown digest grouped by category/theme → written to file → (unless `--no-send` flag is set) markdown content converted to HTML → sent via email to configured recipient

## Key Functions
- `synthesize_news(item, config)`: Calls LiteLLM with category, feed, title, URL and content; instructs the LLM to produce a **comprehensive synthesis** that captures most if not all essential information (main points, context, data/statistics, quotes, implications, dates/locations/entities, technical details); always responds in English regardless of the source language; returns synthesis text
- `update_db_content(item_id, synthesis_markdown, config)`: Converts synthesis Markdown to HTML and writes it to `entry.content` in the SQLite DB; the `.md` file on disk is left unchanged
- `process_regular_news(item, config)`: Creates `{data.directory}/news/` dir if needed, writes synthesis to `{id}.md`, then calls `update_db_content`
- `get_youtube_video_id(url)`: Extracts the YouTube video ID from a URL (supports `youtube.com/watch?v=`, `youtu.be/`, and `m.youtube.com` formats)
- `get_youtube_transcript(item, config)`: Downloads transcript via `fetchtranscript.com` API (uses `config["fetchtranscript"]["api_key"]`), uses GET request to `/v1/transcripts/{video_id}?format=text`, writes plain text to `{data.directory}/youtube/{id}.txt`; skips if file already exists; returns path or `None`
- `process_youtube_news(item, config)`: Entry point for YouTube items; skips if synthesis (`{data.directory}/news/{id}.md`) already exists; otherwise calls `get_youtube_transcript` (which reuses cached `.txt` if present), synthesizes the transcript via LLM, writes to `{data.directory}/news/{id}.md`, and calls `update_db_content`. The transcript and synthesis are tracked independently — a cached transcript does **not** suppress synthesis if the `.md` file is missing. Logs "cached" vs "fetched" at INFO level so the transcript source is always visible. Also guards against empty transcript files before calling the LLM.
- `get_podcast_transcript(item, config)`: Passes the podcast URL directly to AssemblyAI for transcription, writes plain text to `{data.directory}/podcasts/{id}.txt`; skips if file already exists; returns path or `None`
- `process_podcast_news(item, config)`: Entry point for podcast items; calls `get_podcast_transcript`, then synthesizes via LLM and writes to `{data.directory}/news/{id}.md`; skips if synthesis already exists; calls `update_db_content` after writing
- `collect_period_syntheses(news_items, config)`: Reads all existing `{data.directory}/news/{id}.md` files for the period's news items (skipping ignored feeds and missing files); returns a list of dicts with synthesis text and metadata
- `make_global_synthesis(syntheses, frequency, config)`: Sends all collected syntheses to the LLM with instructions to group by category/theme and produce a **comprehensive structured Markdown digest** that captures essential information from all items; allows subsections (###) and bullet points for organizing detailed content; returns the synthesis string or `None`
- `write_global_synthesis(synthesis_text, frequency, config)`: Creates `{data.directory}/synthesis/` dir if needed; writes the global synthesis to `YYYY-MM-DD.md` (daily) or `YYYY-Week_XX.md` (weekly, using ISO year and week number); skips if file already exists; returns the file path
- `send_email(subject, content, config)`: Sends an email using SMTP configuration from config.yaml; converts markdown content to HTML (with tables support) and sends both plain text and HTML versions; uses SMTP_SSL if ssl=true, otherwise uses SMTP with STARTTLS; returns True on success, False on failure
- LiteLLM is called with `openai/{model_name}` prefix to route to the configured OpenAI-compatible endpoint

## Important
- **Update this AGENTS.md file after any relevant changes** (new features, bug fixes, configuration changes)

## Lessons Learned
- **AssemblyAI speech model**: The AssemblyAI API no longer accepts the generic `"universal"` model name. Use `speech_models=["universal-3-pro", "universal-2"]` with `language_detection=True` in `TranscriptionConfig`. The SDK's `SpeechModel.universal` enum resolves to `"universal"` which is now rejected by the API.
- **AssemblyAI URL transcription**: No need to download podcast audio locally first. AssemblyAI can transcribe directly from a public URL — pass `item["link"]` straight to `transcriber.transcribe()`.
- **Logging system**: The project uses Python's built-in logging module with standardized format `YYYY-MM-DD HH:MM:SS [LEVEL] module_name - message`. All failures are logged with appropriate levels (ERROR for critical failures, WARNING for non-critical issues). Use `--log-level` argument to control verbosity.
- **YouTube transcript vs synthesis independence**: The transcript cache (`data/youtube/ID.txt`) and the synthesis file (`data/news/ID.md`) are independent. A cached transcript must not suppress synthesis — only the existence of the `.md` file should skip synthesis. `process_youtube_news` checks `synthesis_path` first; `get_youtube_transcript` is called afterwards and simply returns the cached path if the `.txt` exists. Also guard against empty transcript files (`.strip()` check) before sending to the LLM.
