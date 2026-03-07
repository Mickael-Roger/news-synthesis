import argparse
import os
import re
import smtplib
import subprocess
import tempfile
import yaml
import sqlite3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import assemblyai as aai
import litellm
import markdown


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
    llm_config = config["llm"]
    model = f"openai/{llm_config['model_name']}"

    prompt = (
        f"Category: {item['category_name'] or 'Uncategorized'}\n"
        f"Feed: {item['feed_name']}\n"
        f"Title: {item['title']}\n"
        f"URL: {item['link']}\n\n"
        f"Content:\n{item['content'] or ''}\n\n"
        "Please provide a concise synthesis of this news article. "
        "Regardless of the original language, your synthesis must always be written in English."
    )

    response = litellm.completion(
        model=model,
        api_key=llm_config["api_key"],
        api_base=llm_config["endpoint"],
        messages=[{"role": "user", "content": prompt}],
    )

    return response.choices[0].message.content


def update_db_content(item_id, synthesis_markdown, config):
    """Convert *synthesis_markdown* to HTML and store it in entry.content.

    The on-disk .md file is left untouched; only the SQLite database row is
    updated so that readers of the DB (e.g. FreshRSS) see the rendered HTML.
    """
    html_content = markdown.markdown(synthesis_markdown, extensions=["tables"])
    sqlite_path = config["data"]["sqlite_path"]
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(
            "UPDATE entry SET content = ? WHERE id = ?",
            (html_content, item_id),
        )
        conn.commit()
    except Exception as e:
        pass
    finally:
        conn.close()


def process_regular_news(item, config):
    """Create a markdown synthesis file for a regular (non-YouTube, non-podcast) news item."""
    news_dir = os.path.join(config["data"]["directory"], "news")
    os.makedirs(news_dir, exist_ok=True)

    file_path = os.path.join(news_dir, f"{item['id']}.md")

    if os.path.exists(file_path):
        return

    synthesis = synthesize_news(item, config)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(synthesis)

    update_db_content(item["id"], synthesis, config)
    print(f"Synthesized: [{item['feed_name']}] {item['title']}")


def parse_vtt(vtt_path):
    """Extract raw conversation text from a VTT subtitle file.

    Auto-generated VTT files contain duplicate/rolling lines and inline
    word-level timecodes like <00:00:01.000><c>word</c>.  This function
    keeps only the clean plain-text lines (no inline tags), removes
    duplicates that arise from the rolling-caption pattern, and joins
    everything into a single readable transcript string.
    """
    with open(vtt_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    clean_lines = []
    for line in lines:
        # Skip header lines, blank lines, and timestamp lines
        if not line.strip():
            continue
        if (
            line.startswith("WEBVTT")
            or line.startswith("Kind:")
            or line.startswith("Language:")
        ):
            continue
        if re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3} -->", line):
            continue
        # Skip lines containing inline word-level timecodes (<HH:MM:SS.mmm><c>…</c>)
        if re.search(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", line):
            continue
        # Strip any remaining HTML-like tags (e.g. <c>, </c>)
        text = re.sub(r"<[^>]+>", "", line).strip()
        if text:
            clean_lines.append(text)

    # Remove consecutive duplicates produced by the rolling-caption pattern
    deduped = []
    for line in clean_lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    return " ".join(deduped)


def get_youtube_transcript(item, config):
    """Download auto-generated subtitles for a YouTube video and return the
    path to the saved plain-text transcript file.

    The transcript is stored at {data.directory}/youtube/{id}.txt.
    If the file already exists it is returned immediately without re-downloading.

    Returns the path to the transcript file, or None if subtitles could not
    be obtained.
    """
    youtube_dir = os.path.join(config["data"]["directory"], "youtube")
    os.makedirs(youtube_dir, exist_ok=True)

    transcript_path = os.path.join(youtube_dir, f"{item['id']}.txt")

    if os.path.exists(transcript_path):
        return transcript_path

    video_url = item["link"]

    # Try English first, then French
    for lang in ("en", "fr"):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [
                "yt-dlp",
                "--write-auto-sub",
                "--skip-download",
                "--sub-lang",
                lang,
                "--output",
                os.path.join(tmpdir, "%(id)s"),
                video_url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)

            # Find the downloaded VTT file
            vtt_files = [
                os.path.join(tmpdir, f)
                for f in os.listdir(tmpdir)
                if f.endswith(".vtt")
            ]

            if not vtt_files:
                continue  # Try next language

            vtt_path = vtt_files[0]
            transcript_text = parse_vtt(vtt_path)

            if not transcript_text.strip():
                continue  # Empty transcript, try next language

            with open(transcript_path, "w", encoding="utf-8") as f:
                f.write(transcript_text)

            return transcript_path

    return None


def process_youtube_news(item, config):
    """Download the transcript for a YouTube video, synthesize it via LLM,
    and write the synthesis to {data.directory}/news/{id}.md."""
    news_dir = os.path.join(config["data"]["directory"], "news")
    os.makedirs(news_dir, exist_ok=True)

    synthesis_path = os.path.join(news_dir, f"{item['id']}.md")
    if os.path.exists(synthesis_path):
        return

    transcript_path = get_youtube_transcript(item, config)
    if transcript_path is None:
        return

    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript_text = f.read()

    # Synthesize using the transcript as content
    item_with_transcript = dict(item)
    item_with_transcript["content"] = transcript_text

    synthesis = synthesize_news(item_with_transcript, config)

    with open(synthesis_path, "w", encoding="utf-8") as f:
        f.write(synthesis)

    update_db_content(item["id"], synthesis, config)
    print(f"Synthesized: [{item['feed_name']}] {item['title']}")


def get_podcast_transcript(item, config):
    """Transcribe a podcast episode via AssemblyAI and return the path to the
    saved plain-text transcript file.

    The transcript is stored at {data.directory}/podcasts/{id}.txt.
    If the file already exists it is returned immediately without re-transcribing.

    Returns the path to the transcript file, or None if transcription failed.
    """
    podcasts_dir = os.path.join(config["data"]["directory"], "podcasts")
    os.makedirs(podcasts_dir, exist_ok=True)

    transcript_path = os.path.join(podcasts_dir, f"{item['id']}.txt")

    if os.path.exists(transcript_path):
        return transcript_path

    audio_url = item["link"]
    aai.settings.api_key = config["assemblyai"]["api_key"]
    transcription_config = aai.TranscriptionConfig(
        speech_models=["universal-3-pro", "universal-2"],
        language_detection=True,
    )
    transcriber = aai.Transcriber(config=transcription_config)
    transcript = transcriber.transcribe(audio_url)

    if transcript.status == aai.TranscriptStatus.error:
        return None

    transcript_text = transcript.text or ""
    if not transcript_text.strip():
        return None

    with open(transcript_path, "w", encoding="utf-8") as f:
        f.write(transcript_text)

    return transcript_path


def process_podcast_news(item, config):
    """Transcribe a podcast episode via AssemblyAI, synthesize it via LLM,
    and write the synthesis to {data.directory}/podcasts/{id}.md.

    Skips processing if the synthesis file already exists.
    """
    news_dir = os.path.join(config["data"]["directory"], "news")
    os.makedirs(news_dir, exist_ok=True)

    synthesis_path = os.path.join(news_dir, f"{item['id']}.md")
    if os.path.exists(synthesis_path):
        return

    transcript_path = get_podcast_transcript(item, config)
    if transcript_path is None:
        return

    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript_text = f.read()

    # Synthesize using the transcript as content
    item_with_transcript = dict(item)
    item_with_transcript["content"] = transcript_text

    synthesis = synthesize_news(item_with_transcript, config)

    with open(synthesis_path, "w", encoding="utf-8") as f:
        f.write(synthesis)

    update_db_content(item["id"], synthesis, config)
    print(f"Synthesized: [{item['feed_name']}] {item['title']}")


def collect_period_syntheses(news_items, config):
    """Collect all per-item synthesis texts for the given set of news items.

    Reads every {data.directory}/news/{id}.md that exists and returns a list
    of dicts with keys: id, title, category_name, feed_name, link, content
    (where content is the synthesis text).  Items whose synthesis file does
    not exist (e.g. transcript unavailable) are silently skipped.
    """
    news_dir = os.path.join(config["data"]["directory"], "news")
    ignored_feeds = config.get("ignored_feeds") or []
    syntheses = []

    for item in news_items:
        if item["feed_name"] in ignored_feeds:
            continue

        file_path = os.path.join(news_dir, f"{item['id']}.md")
        if not os.path.exists(file_path):
            continue

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

    return syntheses


def make_global_synthesis(syntheses, frequency, config):
    """Send all per-item syntheses to the LLM and return a global synthesis.

    The LLM is instructed to group the news by category or theme of its own
    choosing and produce a structured Markdown document summarising the entire
    period.  The response is always in English.

    Returns the synthesis string, or None if there is nothing to synthesise.
    """
    if not syntheses:
        return None

    llm_config = config["llm"]
    model = f"openai/{llm_config['model_name']}"

    period_label = "daily" if frequency == "daily" else "weekly"

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
        f"2. Write a concise global synthesis for each group.\n"
        f"3. Produce a well-structured Markdown document with a title, one section per group "
        f"(using ## headings), and a brief introduction paragraph.\n"
        f"4. Regardless of the original language of any article, your entire response must be in English.\n"
    )

    response = litellm.completion(
        model=model,
        api_key=llm_config["api_key"],
        api_base=llm_config["endpoint"],
        messages=[{"role": "user", "content": prompt}],
    )

    return response.choices[0].message.content


def write_global_synthesis(synthesis_text, frequency, config):
    """Persist the global synthesis to {data.directory}/synthesis/.

    File naming:
      - daily:  YYYY-MM-DD.md   (today's date)
      - weekly: YYYY-Week_XX.md (ISO year and week number)

    Skips writing if the file already exists.
    Returns the path that was written (or already existed).
    """
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
        print(f"  Global synthesis already exists: {file_path}")
        return file_path

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(synthesis_text)

    print(f"  Global synthesis written: {file_path}")
    return file_path


def send_email(subject, content, config):
    """Send an email with the given subject and content.

    Uses SMTP configuration from config.yaml.
    Returns True if sent successfully, False otherwise.
    """
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

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_config["server"], port)
        else:
            server = smtplib.SMTP(smtp_config["server"], port)
            server.starttls()
        server.login(smtp_config["username"], smtp_config["password"])
        server.sendmail(sender, recipient, message.as_string())
        server.quit()
        print(f"  Email sent to {recipient}")
        return True
    except Exception as e:
        print(f"  Failed to send email: {e}")
        return False


def get_latest_news(config, frequency):
    sqlite_path = config["data"]["sqlite_path"]
    seconds_offset = get_time_offset(frequency)

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

    return rows


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
    args = parser.parse_args()

    if args.daily and args.weekly:
        parser.error("Cannot specify both --daily and --weekly")

    frequency = "weekly" if args.weekly else "daily"

    config = load_config(args.config)
    news_items = get_latest_news(config, frequency)

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

    print(f"Found {len(news_items)} news items ({frequency})")
    print(
        f"  YouTube: {youtube_count}, Podcasts: {podcast_count}, Regular: {regular_count}"
    )
    print()

    for item in news_items:
        category = item["category_name"] or "Uncategorized"
        news_type = classify_news(category, config)

        ignored_feeds = config.get("ignored_feeds") or []
        if item["feed_name"] in ignored_feeds:
            continue

        if news_type == "regular":
            process_regular_news(item, config)
        elif news_type == "youtube":
            process_youtube_news(item, config)
        elif news_type == "podcast":
            process_podcast_news(item, config)

    # --- Global synthesis phase ---
    print()
    print("=== Global synthesis phase ===")
    syntheses = collect_period_syntheses(news_items, config)
    print(f"  Collected {len(syntheses)} individual synthesis files.")

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
            print(f"  Global synthesis already exists: {synthesis_path}")
            with open(synthesis_path, "r", encoding="utf-8") as f:
                global_text = f.read()
            print()
            print("=== Email sending phase ===")
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
                print("  Skipping email sending (--no-send flag set)")
        else:
            global_text = make_global_synthesis(syntheses, frequency, config)
            if global_text:
                write_global_synthesis(global_text, frequency, config)
                print()
                print("=== Email sending phase ===")
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
                    print("  Skipping email sending (--no-send flag set)")
            else:
                print("  Warning: LLM returned an empty global synthesis.")
    else:
        print("  No synthesis files found for this period — skipping global synthesis.")


if __name__ == "__main__":
    main()
