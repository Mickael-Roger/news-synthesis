import argparse
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


def synthesize_news(item, config):
    """Send news details to the LLM and return a synthesis."""
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
        "Your task: Provide a comprehensive synthesis of this news article that captures most if not all essential information. "
        "Include the following elements:\n"
        "1. Main points and key facts\n"
        "2. Context and background information\n"
        "3. Specific details, data, statistics, and numbers\n"
        "4. Important quotes or statements\n"
        "5. Implications and consequences\n"
        "6. Relevant dates, locations, and entities mentioned\n"
        "7. Any technical details or specifics that add depth\n"
        "\n"
        "Structure your synthesis clearly with paragraphs or bullet points as appropriate. "
        "Focus on completeness and depth rather than brevity. "
        "Regardless of the original language, your synthesis must always be written in English."
    )

    try:
        response = litellm.completion(
            model=model,
            api_key=llm_config["api_key"],
            api_base=llm_config["endpoint"],
            messages=[{"role": "user", "content": prompt}],
        )
        logger.debug(f"LLM synthesis completed for item: {item['id']}")
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"Failed to synthesize news item {item['id']}: {e}")
        raise


def update_db_content(item_id, synthesis_markdown, config):
    """Convert *synthesis_markdown* to HTML and store it in entry.content.

    The on-disk .md file is left untouched; only the SQLite database row is
    updated so that readers of the DB (e.g. FreshRSS) see the rendered HTML.
    """
    logger = logging.getLogger("news-synthesis")
    html_content = markdown.markdown(synthesis_markdown, extensions=["tables"])
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
        f"Your task:\n"
        f"1. Group the items by category or theme — you choose the grouping that makes the most sense.\n"
        f"2. Write a comprehensive global synthesis for each group that captures:\n"
        f"   - All main points and key facts from each item\n"
        f"   - Important context and background information\n"
        f"   - Specific data, statistics, and numbers mentioned\n"
        f"   - Key quotes and statements\n"
        f"   - Implications, consequences, and technical details\n"
        f"   - Relevant dates, locations, entities, and specifics\n"
        f"3. Ensure that no essential information is lost. Focus on completeness and depth.\n"
        f"4. Produce a well-structured Markdown document with a clear title, one section per group "
        f"(using ## headings), and an introductory paragraph that sets the context.\n"
        f"5. Within each section, you may use subsections (###) or bullet points to organize detailed information.\n"
        f"6. Regardless of the original language of any article, your entire response must be in English.\n"
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


def send_email(subject, content, config):
    """Send an email with the given subject and content.

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

    html_content = markdown.markdown(content, extensions=["tables"])
    html_body = f"""<!DOCTYPE html>
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
