import argparse
import json
import logging
import os
import smtplib
import sys
import yaml
import sqlite3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import assemblyai as aai
import litellm
import markdown
import requests


def setup_logging(log_level=None):
    """Configure logging with a standardized format.

    Format: YYYY-MM-DD HH:MM:SS [LEVEL] module_name - message

    Args:
        log_level: Optional log level as string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
                   Defaults to INFO if not specified.
    """
    level = getattr(logging, log_level.upper()) if log_level else logging.INFO

    log_format = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=level,
        format=log_format,
        datefmt=date_format,
        stream=sys.stdout,
    )

    return logging.getLogger("news-synthesis")


def load_config(config_path):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_time_offset(frequency):
    if frequency == "daily":
        return timedelta(hours=24).total_seconds()
    elif frequency == "weekly":
        return timedelta(days=7).total_seconds()
    else:
        raise ValueError(f"Unknown frequency: {frequency}")


def classify_news(category_name, config):
    youtube_categories = config["feed_categories"]["youtube_category"]
    podcast_categories = config["feed_categories"]["podcast_category"]

    if category_name in youtube_categories:
        return "youtube"
    elif category_name in podcast_categories:
        return "podcast"
    else:
        return "regular"


def fetch_article_content(url):
    """Fetch the article content from a URL and return its text.

    Used as a fallback when the LLM cannot access the article content
    (e.g., some news providers block LLM platform IP ranges).

    Args:
        url: The article URL to fetch.

    Returns:
        The raw HTML content of the page, or an error message if fetching fails.
    """
    logger = logging.getLogger("news-synthesis")
    try:
        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) "
                    "Gecko/20100101 Firefox/128.0"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            timeout=30,
        )
        response.raise_for_status()
        logger.info(f"Fetched article content from URL: {url}")
        return response.text
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout fetching article content from: {url}")
        return "Error: Request timed out while fetching the article."
    except requests.exceptions.RequestException as e:
        logger.warning(f"Failed to fetch article content from {url}: {e}")
        return f"Error: Failed to fetch the article - {e}"


# Tool definition for LLM function calling
FETCH_ARTICLE_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_article_content",
        "description": (
            "Fetch the full article content from the given URL. "
            "Use this tool when the content provided in the prompt is empty, "
            "insufficient, or cannot be used to produce a meaningful synthesis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the article to fetch.",
                }
            },
            "required": ["url"],
        },
    },
}


def synthesize_news(item, config):
    """Send news details to the LLM and return a synthesis.

    The LLM is provided with a ``fetch_article_content`` tool it can invoke
    when the RSS content is empty, truncated, or otherwise insufficient.
    If the LLM calls the tool, the article is fetched via ``requests`` and
    the result is sent back so the LLM can complete the synthesis.
    """
    logger = logging.getLogger("news-synthesis")
    llm_config = config["llm"]
    model = f"openai/{llm_config['model_name']}"

    logger.debug(f"Synthesizing news item: {item['id']} - {item['title']}")

    prompt = (
        f"Category: {item['category_name'] or 'Uncategorized'}\n"
        f"Feed: {item['feed_name']}\n"
        f"Title: {item['title']}\n"
        f"URL: {item['link']}\n\n"
        f"Content:\n{item['content'] or ''}\n\n"
        "Your task: Produce a two-part synthesis of this news article. "
        "Prioritise information density over prose — use bullet points wherever possible.\n"
        "\n"
        "PART 1 — TL;DR (2 to 3 bullet points max): The absolute essentials. What happened, who is involved, why it matters.\n"
        "\n"
        "PART 2 — Detailed breakdown: Extract every piece of useful information as concise bullet points. "
        "Cover:\n"
        "- Key facts and main points\n"
        "- Context and background\n"
        "- Data, statistics, and numbers\n"
        "- Notable quotes or statements (quote directly if relevant)\n"
        "- Implications and consequences\n"
        "- Dates, locations, entities\n"
        "- Technical details\n"
        "\n"
        "IMPORTANT: If the content provided above is empty, unavailable, or insufficient to produce a meaningful synthesis, "
        "use the fetch_article_content tool to retrieve the full article from the URL. "
        "Do NOT invent, fabricate, or hallucinate any information. Only synthesize what is explicitly present in the content.\n"
        "\n"
        "Format your response with '## Summary' as the heading for Part 1 and '## Detailed Synthesis' as the heading for Part 2. "
        "Use bullet points throughout. Avoid full paragraphs of prose. "
        "Regardless of the original language, your entire response must always be written in English."
    )

    try:
        messages = [{"role": "user", "content": prompt}]

        response = litellm.completion(
            model=model,
            api_key=llm_config["api_key"],
            api_base=llm_config["endpoint"],
            messages=messages,
            tools=[FETCH_ARTICLE_TOOL],
            tool_choice="auto",
        )

        response_message = response.choices[0].message

        # Check if the LLM wants to call the fetch tool
        if response_message.tool_calls:
            tool_call = response_message.tool_calls[0]
            if tool_call.function.name == "fetch_article_content":
                args = json.loads(tool_call.function.arguments)
                url = args.get("url", item["link"])
                logger.info(f"LLM requested article fetch for item {item['id']}: {url}")
                fetched_content = fetch_article_content(url)

                # Send the tool result back to the LLM
                messages.append(response_message.model_dump())
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": fetched_content,
                    }
                )

                response = litellm.completion(
                    model=model,
                    api_key=llm_config["api_key"],
                    api_base=llm_config["endpoint"],
                    messages=messages,
                    tools=[FETCH_ARTICLE_TOOL],
                )
                logger.debug(
                    f"LLM synthesis completed (after fetch) for item: {item['id']}"
                )
                return response.choices[0].message.content

        logger.debug(f"LLM synthesis completed for item: {item['id']}")
        return response_message.content
    except Exception as e:
        logger.error(f"Failed to synthesize news item {item['id']}: {e}")
        raise


def convert_news_synthesis_to_html(synthesis_markdown, config):
    """Convert a per-item news synthesis from Markdown to styled HTML using the small LLM.

    Falls back to basic ``markdown`` library conversion if the LLM call fails.

    Args:
        synthesis_markdown: The Markdown synthesis text to convert.
        config: The full config dict (needs config["llm"]).

    Returns:
        An HTML string (always succeeds — worst case is basic conversion).
    """
    logger = logging.getLogger("news-synthesis")
    llm_config = config["llm"]
    model = f"openai/{llm_config['small_model_name']}"

    prompt = (
        "Convert the following Markdown news synthesis into clean, well-formatted HTML "
        "suitable for display in an RSS reader.\n\n"
        "REQUIREMENTS:\n"
        "1. Produce only the HTML body content — no <!DOCTYPE>, <html>, <head>, or <body> tags.\n"
        "2. Use inline CSS styles where helpful for readability (e.g. for headings, bullet lists).\n"
        "3. Keep the content in English exactly as provided — do NOT translate.\n"
        '4. Preserve all Markdown links as proper <a href="..."> HTML links.\n'
        "5. Style headings (## Summary, ## Detailed Synthesis) clearly.\n"
        "6. Output ONLY the HTML fragment. No explanations, no markdown fences, no commentary.\n\n"
        f"MARKDOWN CONTENT:\n\n{synthesis_markdown}"
    )

    try:
        response = litellm.completion(
            model=model,
            api_key=llm_config["api_key"],
            api_base=llm_config["endpoint"],
            messages=[{"role": "user", "content": prompt}],
        )
        html_output = response.choices[0].message.content

        # Strip markdown code fences if the LLM wrapped the output
        if html_output.startswith("```"):
            lines = html_output.split("\n")
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            html_output = "\n".join(lines)

        logger.debug("News synthesis HTML conversion via LLM completed")
        return html_output
    except Exception as e:
        logger.warning(
            f"LLM HTML conversion failed for news synthesis, falling back to markdown library: {e}"
        )
        return markdown.markdown(synthesis_markdown, extensions=["tables"])


def update_db_content(item_id, synthesis_markdown, config):
    """Convert *synthesis_markdown* to HTML via the small LLM and store it in entry.content.

    The on-disk .md file is left untouched; only the SQLite database row is
    updated so that readers of the DB (e.g. FreshRSS) see the rendered HTML.
    Falls back to basic markdown-to-HTML conversion if the LLM call fails.
    """
    logger = logging.getLogger("news-synthesis")
    html_content = convert_news_synthesis_to_html(synthesis_markdown, config)
    sqlite_path = config["data"]["sqlite_path"]
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "UPDATE entry SET content = ? WHERE id = ?",
            (html_content, item_id),
        )
        conn.commit()
        logger.debug(f"Updated database content for entry: {item_id}")
    except Exception as e:
        logger.error(f"Failed to update database content for entry {item_id}: {e}")
    finally:
        conn.close()


def process_regular_news(item, config):
    """Create a markdown synthesis file for a regular (non-YouTube, non-podcast) news item."""
    logger = logging.getLogger("news-synthesis")
    news_dir = os.path.join(config["data"]["directory"], "news")
    os.makedirs(news_dir, exist_ok=True)

    file_path = os.path.join(news_dir, f"{item['id']}.md")

    if os.path.exists(file_path):
        logger.debug(f"Skipping synthesis (already exists): {item['id']}")
        return

    try:
        synthesis = synthesize_news(item, config)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(synthesis)

        update_db_content(item["id"], synthesis, config)
        logger.info(f"Synthesized: [{item['feed_name']}] {item['title']}")
    except Exception as e:
        logger.error(f"Failed to process regular news item {item['id']}: {e}")
        raise


def get_youtube_video_id(url):
    """Extract the YouTube video ID from a URL."""
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    if parsed.hostname in ("youtu.be",):
        return parsed.path.lstrip("/")
    if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        params = urllib.parse.parse_qs(parsed.query)
        return params.get("v", [None])[0]
    return None


def get_youtube_transcript(item, config):
    """Fetch the transcript for a YouTube video via fetchtranscript API and
    return the path to the saved plain-text transcript file.

    The transcript is stored at {data.directory}/youtube/{id}.txt.
    If the file already exists it is returned immediately without re-fetching.

    Returns the path to the transcript file, or None if no transcript could be
    obtained.
    """
    logger = logging.getLogger("news-synthesis")
    youtube_dir = os.path.join(config["data"]["directory"], "youtube")
    os.makedirs(youtube_dir, exist_ok=True)

    transcript_path = os.path.join(youtube_dir, f"{item['id']}.txt")

    if os.path.exists(transcript_path):
        logger.debug(f"Using existing transcript: {transcript_path}")
        return transcript_path

    video_id = get_youtube_video_id(item["link"])
    if not video_id:
        logger.error(f"Could not extract YouTube video ID from URL: {item['link']}")
        return None

    logger.info(f"Fetching YouTube transcript for: {item['id']} - {item['title']}")

    try:
        response = requests.get(
            f"https://api.fetchtranscript.com/v1/transcripts/{video_id}",
            params={"format": "text"},
            headers={"Authorization": f"Bearer {config['fetchtranscript']['api_key']}"},
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        transcript_text = data.get("text", "")
    except requests.exceptions.Timeout:
        logger.error(
            f"Timeout fetching YouTube transcript for item {item['id']}: video_id {video_id}"
        )
        return None
    except requests.exceptions.RequestException as e:
        logger.error(
            f"Failed to fetch YouTube transcript for item {item['id']}: video_id {video_id} - {e}"
        )
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error fetching YouTube transcript for item {item['id']}: {e}"
        )
        return None

    if not transcript_text or not transcript_text.strip():
        logger.warning(f"Empty transcript received for YouTube item {item['id']}")
        return None

    transcript_text = transcript_text.replace("\n", " ")

    try:
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript_text)
        logger.debug(f"Transcript saved: {transcript_path}")
    except Exception as e:
        logger.error(f"Failed to write transcript file {transcript_path}: {e}")
        return None

    return transcript_path


def process_youtube_news(item, config):
    """Download the transcript for a YouTube video, synthesize it via LLM,
    and write the synthesis to {data.directory}/news/{id}.md.

    The transcript ({data.directory}/youtube/{id}.txt) is fetched only once and
    reused on subsequent runs.  The synthesis ({data.directory}/news/{id}.md) is
    independent: even when the transcript already exists, synthesis is still
    performed if the .md file is missing.
    """
    logger = logging.getLogger("news-synthesis")
    news_dir = os.path.join(config["data"]["directory"], "news")
    os.makedirs(news_dir, exist_ok=True)

    synthesis_path = os.path.join(news_dir, f"{item['id']}.md")
    if os.path.exists(synthesis_path):
        logger.debug(f"Skipping synthesis (already exists): {item['id']}")
        return

    # Fetch the transcript (no-op if the .txt file already exists on disk)
    transcript_path = get_youtube_transcript(item, config)
    if transcript_path is None:
        logger.warning(
            f"Skipping YouTube synthesis: no transcript available for item {item['id']}"
        )
        return

    logger.info(
        f"Synthesizing YouTube item (transcript {'cached' if os.path.exists(transcript_path) else 'fetched'}): "
        f"[{item['feed_name']}] {item['title']}"
    )

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_text = f.read()

        if not transcript_text.strip():
            logger.warning(
                f"Transcript file is empty for YouTube item {item['id']}, skipping synthesis"
            )
            return

        # Synthesize using the transcript as content
        item_with_transcript = dict(item)
        item_with_transcript["content"] = transcript_text

        synthesis = synthesize_news(item_with_transcript, config)

        with open(synthesis_path, "w", encoding="utf-8") as f:
            f.write(synthesis)

        update_db_content(item["id"], synthesis, config)
        logger.info(f"Synthesized: [{item['feed_name']}] {item['title']}")
    except Exception as e:
        logger.error(f"Failed to process YouTube news item {item['id']}: {e}")
        raise


def _transcribe_via_local_download(audio_url, item_id, transcriber, config):
    """Download *audio_url* locally and submit the local file to AssemblyAI.

    Used as a fallback when AssemblyAI cannot fetch the URL itself (e.g. the
    server rejects its download agent).  The SDK uploads the local file
    automatically.

    Returns the AssemblyAI Transcript object, or None on failure.
    """
    import tempfile
    import mimetypes

    logger = logging.getLogger("news-synthesis")

    # Mimic a podcast client to avoid CDN blocks (e.g. CloudFront filtering
    # requests from unknown user agents)
    headers = {
        "User-Agent": ("PodcastAddict/v5 (+https://podcastaddict.com/app) Android/14")
    }

    logger.info(f"Downloading audio locally for item {item_id}: {audio_url}")
    try:
        response = requests.get(audio_url, headers=headers, timeout=120, stream=True)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download audio for item {item_id}: {e}")
        return None

    # Verify the content looks like audio
    content_type = response.headers.get("Content-Type", "")
    if content_type and not (
        content_type.startswith("audio/")
        or content_type.startswith("video/")
        or "octet-stream" in content_type
    ):
        logger.error(
            f"Unexpected content type '{content_type}' when downloading audio for item {item_id}"
        )
        return None

    # Determine a sensible file extension
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip()) or ".mp3"
    # mimetypes sometimes returns .mp2 for audio/mpeg — normalise to .mp3
    if ext in (".mp2", ".mpga"):
        ext = ".mp3"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp_path = tmp.name
            for chunk in response.iter_content(chunk_size=8192):
                tmp.write(chunk)

        logger.info(
            f"Audio downloaded to temporary file: {tmp_path} for item {item_id}"
        )
        transcript = transcriber.transcribe(tmp_path)
        return transcript
    except Exception as e:
        logger.error(f"Error during local-file transcription for item {item_id}: {e}")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
                logger.debug(f"Temporary audio file removed: {tmp_path}")
            except OSError as e:
                logger.warning(f"Could not remove temporary file {tmp_path}: {e}")


def get_podcast_transcript(item, config):
    """Transcribe a podcast episode via AssemblyAI and return the path to the
    saved plain-text transcript file.

    The transcript is stored at {data.directory}/podcasts/{id}.txt.
    If the file already exists it is returned immediately without re-transcribing.

    Returns the path to the transcript file, or None if transcription failed.
    """
    logger = logging.getLogger("news-synthesis")
    podcasts_dir = os.path.join(config["data"]["directory"], "podcasts")
    os.makedirs(podcasts_dir, exist_ok=True)

    transcript_path = os.path.join(podcasts_dir, f"{item['id']}.txt")

    if os.path.exists(transcript_path):
        logger.debug(f"Using existing transcript: {transcript_path}")
        return transcript_path

    # Extract the MP3 URL from the enclosure in the attributes field
    audio_url = None
    attributes_raw = item["attributes"] if "attributes" in item.keys() else None
    if attributes_raw:
        try:
            attributes = json.loads(attributes_raw)
            enclosures = attributes.get("enclosures", [])
            if enclosures:
                audio_url = enclosures[0].get("url")
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(
                f"Failed to parse attributes for podcast item {item['id']}: {e}"
            )

    if not audio_url:
        logger.warning(
            f"No enclosure URL found in attributes for podcast item {item['id']}, falling back to link"
        )
        audio_url = item["link"]

    logger.info(f"Transcribing podcast: {item['id']} - {item['title']}")

    try:
        aai.settings.api_key = config["assemblyai"]["api_key"]
        transcription_config = aai.TranscriptionConfig(
            speech_models=["universal-3-pro", "universal-2"],
            language_detection=True,
        )
        transcriber = aai.Transcriber(config=transcription_config)
        transcript = transcriber.transcribe(audio_url)
    except Exception as e:
        logger.error(
            f"Failed to transcribe podcast {item['id']}: URL {audio_url} - {e}"
        )
        return None

    if transcript.status == aai.TranscriptStatus.error:
        error_msg = transcript.error or ""
        if "Download error" in error_msg or "unable to download" in error_msg.lower():
            logger.warning(
                f"AssemblyAI could not download the audio URL for item {item['id']}, "
                f"attempting local download fallback: {audio_url}"
            )
            transcript = _transcribe_via_local_download(
                audio_url, item["id"], transcriber, config
            )
            if transcript is None:
                return None
            if transcript.status == aai.TranscriptStatus.error:
                logger.error(
                    f"AssemblyAI transcription error (local fallback) for item {item['id']}: "
                    f"{transcript.error}"
                )
                return None
        else:
            logger.error(
                f"AssemblyAI transcription error for item {item['id']}: {transcript.error}"
            )
            return None

    transcript_text = transcript.text or ""
    if not transcript_text.strip():
        logger.warning(f"Empty transcript received for podcast item {item['id']}")
        return None

    try:
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript_text)
        logger.debug(f"Transcript saved: {transcript_path}")
    except Exception as e:
        logger.error(f"Failed to write transcript file {transcript_path}: {e}")
        return None

    return transcript_path


def process_podcast_news(item, config):
    """Transcribe a podcast episode via AssemblyAI, synthesize it via LLM,
    and write the synthesis to {data.directory}/podcasts/{id}.md.

    Skips processing if the synthesis file already exists.
    """
    logger = logging.getLogger("news-synthesis")
    news_dir = os.path.join(config["data"]["directory"], "news")
    os.makedirs(news_dir, exist_ok=True)

    synthesis_path = os.path.join(news_dir, f"{item['id']}.md")
    if os.path.exists(synthesis_path):
        logger.debug(f"Skipping synthesis (already exists): {item['id']}")
        return

    transcript_path = get_podcast_transcript(item, config)
    if transcript_path is None:
        logger.warning(
            f"Skipping podcast synthesis: no transcript available for item {item['id']}"
        )
        return

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript_text = f.read()

        # Synthesize using the transcript as content
        item_with_transcript = dict(item)
        item_with_transcript["content"] = transcript_text

        synthesis = synthesize_news(item_with_transcript, config)

        with open(synthesis_path, "w", encoding="utf-8") as f:
            f.write(synthesis)

        update_db_content(item["id"], synthesis, config)
        logger.info(f"Synthesized: [{item['feed_name']}] {item['title']}")
    except Exception as e:
        logger.error(f"Failed to process podcast news item {item['id']}: {e}")
        raise


def collect_period_syntheses(news_items, config):
    """Collect all per-item synthesis texts for the given set of news items.

    Reads every {data.directory}/news/{id}.md that exists and returns a list
    of dicts with keys: id, title, category_name, feed_name, link, content
    (where content is the synthesis text).  Items whose synthesis file does
    not exist (e.g. transcript unavailable) are silently skipped.
    """
    logger = logging.getLogger("news-synthesis")
    news_dir = os.path.join(config["data"]["directory"], "news")
    ignored_feeds = config.get("ignored_feeds") or []
    syntheses = []
    skipped_count = 0

    for item in news_items:
        if item["feed_name"] in ignored_feeds:
            logger.debug(f"Skipping ignored feed: {item['feed_name']}")
            continue

        file_path = os.path.join(news_dir, f"{item['id']}.md")
        if not os.path.exists(file_path):
            skipped_count += 1
            logger.debug(f"Skipping item (no synthesis file): {item['id']}")
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                synthesis_text = f.read()

            syntheses.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "category_name": item["category_name"] or "Uncategorized",
                    "feed_name": item["feed_name"],
                    "link": item["link"],
                    "content": synthesis_text,
                }
            )
        except Exception as e:
            logger.error(f"Failed to read synthesis file for item {item['id']}: {e}")

    if skipped_count > 0:
        logger.info(f"Skipped {skipped_count} items without synthesis files")

    return syntheses


def make_global_synthesis(syntheses, frequency, config):
    """Send all per-item syntheses to the LLM and return a global synthesis.

    The LLM is instructed to group the news by category or theme of its own
    choosing and produce a structured Markdown document summarising the entire
    period.  The response is always in English.

    Returns the synthesis string, or None if there is nothing to synthesise.
    """
    logger = logging.getLogger("news-synthesis")
    if not syntheses:
        return None

    llm_config = config["llm"]
    model = f"openai/{llm_config['model_name']}"

    period_label = "daily" if frequency == "daily" else "weekly"

    logger.info(f"Creating {period_label} global synthesis from {len(syntheses)} items")

    # Build the aggregated content block
    items_block = ""
    for s in syntheses:
        items_block += (
            f"---\n"
            f"Category: {s['category_name']}\n"
            f"Feed: {s['feed_name']}\n"
            f"Title: {s['title']}\n"
            f"URL: {s['link']}\n\n"
            f"{s['content']}\n\n"
        )

    prompt = (
        f"Below are the individual news syntheses collected for the {period_label} digest.\n"
        f"Each synthesis is separated by '---'.\n\n"
        f"{items_block}"
        f"Your task: produce a structured Markdown digest with exactly three sections, in this order.\n\n"
        f"---\n\n"
        f"SECTION 1 — '## Highlights'\n"
        f"Pick the 2 to 3 most important or impactful news items from the entire set. "
        f"Write one bullet point per highlight. Each bullet must be self-contained: state what happened, "
        f"who is involved, and why it matters — all in one or two short sentences. No fluff.\n\n"
        f"---\n\n"
        f"SECTION 2 — '## News'\n"
        f"First, classify every item as either:\n"
        f"  - TYPE A — CURRENT NEWS: a recent event, announcement, or development "
        f"(product launch, model release, funding round, policy change, security incident, etc.).\n"
        f"  - TYPE B — TECH ARTICLE: evergreen content — deep dives, tutorials, opinion pieces, "
        f"how-it-works explanations — not tied to a specific recent event.\n\n"
        f"Include only TYPE A items in this section. Group them under topic subsections (### Topic). "
        f"Choose topic names dynamically based on the actual content "
        f"(e.g. AI, Cloud, Cybersecurity, Science, Business, Geopolitics, Space, Health, …). "
        f"Use as many or as few topics as needed — do not force items into ill-fitting categories.\n\n"
        f"Rules for this section:\n"
        f"- If multiple items cover the same story from different sources, merge them into a single entry.\n"
        f"- Each entry gets 2 to 3 bullet points maximum. Be ruthlessly concise.\n"
        f"- Each bullet must carry a distinct, concrete piece of information (fact, figure, quote, consequence).\n"
        f"- Include the item title as a Markdown link on the first bullet of each entry.\n"
        f"- No prose paragraphs. Bullet points only.\n\n"
        f"---\n\n"
        f"SECTION 3 — '## Tech Articles & Deep Dives'\n"
        f"List all TYPE B items here. For each item write a single bullet point containing:\n"
        f"  - The article title as a Markdown link\n"
        f"  - The feed name in bold (e.g. **Feed Name**)\n"
        f"  - One sentence describing what it covers\n"
        f"Do not write long summaries — the goal is just to surface the article.\n\n"
        f"---\n\n"
        f"Global rules:\n"
        f"- Use bullet points everywhere. No prose paragraphs.\n"
        f"- No TYPE A item may be omitted from Section 2.\n"
        f"- If you are uncertain whether an item is TYPE A or TYPE B, classify it as TYPE A.\n"
        f"- Your entire response must be in English regardless of the source language.\n"
    )

    try:
        response = litellm.completion(
            model=model,
            api_key=llm_config["api_key"],
            api_base=llm_config["endpoint"],
            messages=[{"role": "user", "content": prompt}],
        )
        logger.debug(f"Global synthesis completed")
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Failed to create global synthesis: {e}")
        raise


def write_global_synthesis(synthesis_text, frequency, config):
    """Persist the global synthesis to {data.directory}/synthesis/.

    File naming:
      - daily:  YYYY-MM-DD.md   (today's date)
      - weekly: YYYY-Week_XX.md (ISO year and week number)

    Skips writing if the file already exists.
    Returns the path that was written (or already existed).
    """
    logger = logging.getLogger("news-synthesis")
    synthesis_dir = os.path.join(config["data"]["directory"], "synthesis")
    os.makedirs(synthesis_dir, exist_ok=True)

    now = datetime.now()
    if frequency == "daily":
        filename = now.strftime("%Y-%m-%d.md")
    else:
        iso_year, iso_week, _ = now.isocalendar()
        filename = f"{iso_year}-Week_{iso_week:02d}.md"

    file_path = os.path.join(synthesis_dir, filename)

    if os.path.exists(file_path):
        logger.debug(f"Global synthesis already exists: {file_path}")
        return file_path

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(synthesis_text)
        logger.info(f"Global synthesis written: {file_path}")
    except Exception as e:
        logger.error(f"Failed to write global synthesis file {file_path}: {e}")
        raise

    return file_path


def convert_markdown_to_html_via_llm(markdown_content, subject, config):
    """Convert markdown synthesis to beautiful bilingual HTML using the small LLM model.

    The LLM produces a polished HTML email with the content in both English and
    French (translated by the LLM). Both versions are displayed sequentially,
    separated by a visual divider, for maximum email-client compatibility.

    Args:
        markdown_content: The markdown synthesis text (in English).
        subject: The email subject line, used as the HTML title/header.
        config: The full config dict (needs config["llm"]).

    Returns:
        A complete HTML document string, or None on failure.
    """
    logger = logging.getLogger("news-synthesis")
    llm_config = config["llm"]
    model = f"openai/{llm_config['small_model_name']}"

    prompt = (
        "You are an expert HTML email designer. Convert the following Markdown newsletter "
        "into a beautiful, modern, responsive HTML email.\n\n"
        "REQUIREMENTS:\n"
        "1. Produce a COMPLETE standalone HTML document (<!DOCTYPE html> through </html>).\n"
        "2. Use ALL inline CSS styles (no external stylesheets). Use a clean, professional "
        "newsletter design with good typography, spacing, and visual hierarchy.\n"
        "3. Design guidelines:\n"
        "   - Use a max-width container (680px) centered on the page with a light background.\n"
        "   - Use a colored header/banner area with the email subject as the title.\n"
        "   - Use clear section headings with subtle borders or background colors.\n"
        "   - Use readable fonts (system font stack: -apple-system, BlinkMacSystemFont, "
        "'Segoe UI', Roboto, Helvetica, Arial, sans-serif).\n"
        "   - Use appropriate colors: dark text (#1a1a2e), accent color (#0f3460) for headings, "
        "subtle backgrounds (#f5f5f5, #e8edf2) for sections.\n"
        "   - Style links nicely (colored, underlined on hover).\n"
        "   - Use bullet points, blockquotes, or cards for article items.\n"
        "   - Ensure tables are well-styled if present.\n"
        "4. The email must contain TWO language sections displayed one after the other:\n"
        "   a. FIRST: The original English content, with a small heading/label "
        "indicating 'English Version' or similar.\n"
        "   b. THEN: A clear visual separator/divider.\n"
        "   c. THEN: The FULL French translation of the same content, with a small heading/label "
        "indicating 'Version Francaise' or similar.\n"
        "5. The French translation must be accurate and natural-sounding. Translate EVERYTHING "
        "including section headings, but keep proper nouns, product names, and technical terms "
        "in their original form.\n"
        "6. Output ONLY the HTML code. No explanations, no markdown fences, no commentary.\n\n"
        f"EMAIL SUBJECT: {subject}\n\n"
        f"MARKDOWN CONTENT TO CONVERT AND TRANSLATE:\n\n{markdown_content}"
    )

    logger.info("Converting markdown to bilingual HTML via LLM (small model)")
    try:
        response = litellm.completion(
            model=model,
            api_key=llm_config["api_key"],
            api_base=llm_config["endpoint"],
            messages=[{"role": "user", "content": prompt}],
        )
        html_output = response.choices[0].message.content

        # Strip markdown code fences if the LLM wrapped the output
        if html_output.startswith("```"):
            lines = html_output.split("\n")
            # Remove first line (```html or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            html_output = "\n".join(lines)

        logger.info("HTML conversion via LLM completed successfully")
        return html_output
    except Exception as e:
        logger.error(f"Failed to convert markdown to HTML via LLM: {e}")
        return None


def _fallback_markdown_to_html(content, subject):
    """Fallback HTML conversion using the markdown library (no LLM).

    Used when the LLM-based conversion fails.
    """
    html_content = markdown.markdown(content, extensions=["tables"])
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        h1, h2 {{ color: #2c3e50; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f2f2f2; }}
    </style>
</head>
<body>
{html_content}
</body>
</html>
"""


def send_email(subject, content, config):
    """Send an email with the given subject and content.

    Uses the small LLM model to convert markdown to beautiful bilingual HTML.
    Falls back to basic markdown-to-HTML conversion if LLM fails.
    Uses SMTP configuration from config.yaml.
    Returns True if sent successfully, False otherwise.
    """
    logger = logging.getLogger("news-synthesis")
    smtp_config = config["smtp"]
    sender = smtp_config["sender_email"]
    recipient = smtp_config["destination_email"]
    port = smtp_config.get("port", 587)
    use_ssl = smtp_config.get("ssl", False)

    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient

    text_part = MIMEText(content, "plain", "utf-8")
    message.attach(text_part)

    html_body = convert_markdown_to_html_via_llm(content, subject, config)
    if html_body is None:
        logger.warning(
            "LLM HTML conversion failed, falling back to basic markdown conversion"
        )
        html_body = _fallback_markdown_to_html(content, subject)

    html_part = MIMEText(html_body, "html", "utf-8")
    message.attach(html_part)

    logger.info(f"Sending email to {recipient}")
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_config["server"], port)
        else:
            server = smtplib.SMTP(smtp_config["server"], port)
            server.starttls()
        server.login(smtp_config["username"], smtp_config["password"])
        server.sendmail(sender, recipient, message.as_string())
        server.quit()
        logger.info(f"Email sent successfully to {recipient}")
        return True
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error while sending email to {recipient}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to send email to {recipient}: {e}")
        return False


def get_latest_news(config, frequency):
    logger = logging.getLogger("news-synthesis")
    sqlite_path = config["data"]["sqlite_path"]
    seconds_offset = get_time_offset(frequency)

    logger.info(f"Fetching {frequency} news from database")

    try:
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = """
            SELECT 
                e.id,
                e.title,
                e.link,
                e.author,
                e.content,
                e.date,
                e.attributes,
                f.name as feed_name,
                f.url as feed_url,
                f.kind as feed_kind,
                c.name as category_name
            FROM entry e
            LEFT JOIN feed f ON e.id_feed = f.id
            LEFT JOIN category c ON f.category = c.id
            WHERE e.date >= strftime('%s', 'now') - ?
            ORDER BY e.date DESC
        """

        cursor.execute(query, (int(seconds_offset),))
        rows = cursor.fetchall()
        conn.close()
        logger.info(f"Retrieved {len(rows)} news items from database")
        return rows
    except Exception as e:
        logger.error(f"Failed to fetch news from database: {e}")
        raise


def main():
    parser = argparse.ArgumentParser(
        description="News synthesis from FreshRSS database"
    )
    parser.add_argument("--config", required=True, help="Path to config.yaml file")
    parser.add_argument(
        "--daily", action="store_true", help="Process last 24 hours of news"
    )
    parser.add_argument(
        "--weekly", action="store_true", help="Process last 7 days of news"
    )
    parser.add_argument(
        "--no-send", action="store_true", help="Skip email sending at the end"
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)",
    )
    args = parser.parse_args()

    if args.daily and args.weekly:
        parser.error("Cannot specify both --daily and --weekly")

    frequency = "weekly" if args.weekly else "daily"

    setup_logging(args.log_level)
    logger = logging.getLogger("news-synthesis")

    logger.info(f"Starting news synthesis job ({frequency})")
    logger.info(f"Loading configuration from: {args.config}")

    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    try:
        news_items = get_latest_news(config, frequency)
    except Exception as e:
        logger.error(f"Failed to fetch news items: {e}")
        sys.exit(1)

    youtube_count = sum(
        1
        for item in news_items
        if classify_news(item["category_name"] or "Uncategorized", config) == "youtube"
    )
    podcast_count = sum(
        1
        for item in news_items
        if classify_news(item["category_name"] or "Uncategorized", config) == "podcast"
    )
    regular_count = len(news_items) - youtube_count - podcast_count

    logger.info(
        f"Found {len(news_items)} news items ({frequency}) - "
        f"YouTube: {youtube_count}, Podcasts: {podcast_count}, Regular: {regular_count}"
    )

    for item in news_items:
        category = item["category_name"] or "Uncategorized"
        news_type = classify_news(category, config)

        ignored_feeds = config.get("ignored_feeds") or []
        if item["feed_name"] in ignored_feeds:
            logger.debug(f"Skipping ignored feed: {item['feed_name']}")
            continue

        try:
            if news_type == "regular":
                process_regular_news(item, config)
            elif news_type == "youtube":
                process_youtube_news(item, config)
            elif news_type == "podcast":
                process_podcast_news(item, config)
        except Exception as e:
            logger.error(f"Error processing item {item['id']}: {e}")

    # --- Global synthesis phase ---
    logger.info("=== Global synthesis phase ===")
    syntheses = collect_period_syntheses(news_items, config)

    if syntheses:
        synthesis_dir = os.path.join(config["data"]["directory"], "synthesis")
        os.makedirs(synthesis_dir, exist_ok=True)
        now = datetime.now()
        if frequency == "daily":
            filename = now.strftime("%Y-%m-%d.md")
        else:
            iso_year, iso_week, _ = now.isocalendar()
            filename = f"{iso_year}-Week_{iso_week:02d}.md"
        synthesis_path = os.path.join(synthesis_dir, filename)

        if os.path.exists(synthesis_path):
            logger.debug(f"Global synthesis already exists: {synthesis_path}")
            with open(synthesis_path, "r", encoding="utf-8") as f:
                global_text = f.read()
            logger.info("=== Email sending phase ===")
            period_label = "Daily" if frequency == "daily" else "Weekly"
            if frequency == "daily":
                date_str = now.strftime("%Y-%m-%d")
            else:
                iso_year, iso_week, _ = now.isocalendar()
                date_str = f"{iso_year}-Week_{iso_week:02d}"
            subject = f"{period_label} News Digest - {date_str}"
            if not args.no_send:
                send_email(subject, global_text, config)
            else:
                logger.info("Skipping email sending (--no-send flag set)")
        else:
            try:
                global_text = make_global_synthesis(syntheses, frequency, config)
                if global_text:
                    write_global_synthesis(global_text, frequency, config)
                    logger.info("=== Email sending phase ===")
                    period_label = "Daily" if frequency == "daily" else "Weekly"
                    now = datetime.now()
                    if frequency == "daily":
                        date_str = now.strftime("%Y-%m-%d")
                    else:
                        iso_year, iso_week, _ = now.isocalendar()
                        date_str = f"{iso_year}-Week_{iso_week:02d}"
                    subject = f"{period_label} News Digest - {date_str}"
                    if not args.no_send:
                        send_email(subject, global_text, config)
                    else:
                        logger.info("Skipping email sending (--no-send flag set)")
                else:
                    logger.warning("LLM returned an empty global synthesis")
            except Exception as e:
                logger.error(f"Failed to create global synthesis: {e}")
    else:
        logger.info(
            "No synthesis files found for this period — skipping global synthesis"
        )

    logger.info(f"News synthesis job completed ({frequency})")


if __name__ == "__main__":
    main()
