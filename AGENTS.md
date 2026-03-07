# Project-Specific Agent Guidelines

## Overview
- Purpose: Python job for news synthesis from podcasts, YouTube videos, and RSS feeds
- Language: Python
- Execution: Runs via command-line with --daily or --weekly flags

## Dependencies
- PyYAML - Configuration file handling
- assemblyai - Podcast transcription
- litellm - LLM usage
- smtplib - Email sending (built-in)
- sqlite3 - Database (built-in)
- yt-dlp - Video/audio download (installed in Docker)

## Rules
- Use English for all code, comments, variable names, and documentation
- Follow PEP 8 style guidelines
- Keep the job lightweight and efficient

## Configuration Structure (config.yaml)
- `assemblyai.api_key`: API key for podcast transcription
- `llm`: LLM configuration (endpoint, model_name, api_key)
- `smtp`: Email configuration (server, username, password, sender_email, destination_email)
- `data`: Data paths (directory, sqlite_path)
- `feed_categories.youtube_category`: List of category names for YouTube content
- `feed_categories.podcast_category`: List of category names for podcast content
- `ignored_feeds`: Optional list of feed names to skip entirely (no synthesis performed)

## Command-Line Arguments
- `--config`: (Required) Path to config.yaml file
- `--daily`: Process last 24 hours of news (default)
- `--weekly`: Process last 7 days of news

## Pipeline Status
- **Regular news**: Fetched from DB → LLM synthesis → written to `{data.directory}/news/{id}.md`
- **YouTube news**: Fetched from DB → `yt-dlp` subtitle download → VTT parsing → raw transcript written to `{data.directory}/youtube/{id}.txt` → LLM synthesis → written to `{data.directory}/news/{id}.md`
- **Podcast news**: Fetched from DB → AssemblyAI transcription (direct URL) → raw transcript written to `{data.directory}/podcasts/{id}.txt` → LLM synthesis → written to `{data.directory}/news/{id}.md`
- **Global synthesis**: All `{data.directory}/news/{id}.md` files for the period → merged and sent to LLM → structured Markdown digest grouped by category/theme → written to `{data.directory}/synthesis/YYYY-MM-DD.md` (daily) or `{data.directory}/synthesis/YYYY-Week_XX.md` (weekly)

## Key Functions
- `synthesize_news(item, config)`: Calls LiteLLM with category, feed, title, URL and content; instructs the LLM to always respond in English regardless of the source language; returns synthesis text
- `process_regular_news(item, config)`: Creates `{data.directory}/news/` dir if needed, writes synthesis to `{id}.md`
- `parse_vtt(vtt_path)`: Parses a WebVTT subtitle file; strips timestamps, inline timecodes, and duplicate rolling-caption lines; returns clean plain text
- `get_youtube_transcript(item, config)`: Downloads auto-generated subtitles via `yt-dlp` (tries `en` then `fr`), parses VTT, writes plain text to `{data.directory}/youtube/{id}.txt`; skips if file already exists; returns path or `None`
- `process_youtube_news(item, config)`: Entry point for YouTube items; calls `get_youtube_transcript`, then synthesizes the transcript via LLM and writes to `{data.directory}/news/{id}.md`; skips if synthesis already exists
- `get_podcast_transcript(item, config)`: Passes the podcast URL directly to AssemblyAI for transcription, writes plain text to `{data.directory}/podcasts/{id}.txt`; skips if file already exists; returns path or `None`
- `process_podcast_news(item, config)`: Entry point for podcast items; calls `get_podcast_transcript`, then synthesizes via LLM and writes to `{data.directory}/news/{id}.md`; skips if synthesis already exists
- `collect_period_syntheses(news_items, config)`: Reads all existing `{data.directory}/news/{id}.md` files for the period's news items (skipping ignored feeds and missing files); returns a list of dicts with synthesis text and metadata
- `make_global_synthesis(syntheses, frequency, config)`: Sends all collected syntheses to the LLM with instructions to group by category/theme and produce a structured Markdown digest; returns the synthesis string or `None`
- `write_global_synthesis(synthesis_text, frequency, config)`: Creates `{data.directory}/synthesis/` dir if needed; writes the global synthesis to `YYYY-MM-DD.md` (daily) or `YYYY-Week_XX.md` (weekly, using ISO year and week number); skips if file already exists
- LiteLLM is called with `openai/{model_name}` prefix to route to the configured OpenAI-compatible endpoint

## Important
- **Update this AGENTS.md file after any relevant changes** (new features, bug fixes, configuration changes)

## Lessons Learned
- **AssemblyAI speech model**: The AssemblyAI API no longer accepts the generic `"universal"` model name. Use `speech_models=["universal-3-pro", "universal-2"]` with `language_detection=True` in `TranscriptionConfig`. The SDK's `SpeechModel.universal` enum resolves to `"universal"` which is now rejected by the API.
- **AssemblyAI URL transcription**: No need to download podcast audio locally first. AssemblyAI can transcribe directly from a public URL — pass `item["link"]` straight to `transcriber.transcribe()`.
