import io
import os
import requests
from bs4 import BeautifulSoup
from docx import Document
from docx.oxml.ns import qn
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_PARAGRAPH_ALIGNMENT
from docx.enum.section import WD_SECTION
from datetime import datetime
import pymongo
from deep_translator import GoogleTranslator
import asyncio
import telegram
import tempfile
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.oxml import OxmlElement
from PIL import Image
import re
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import logging
from google.auth.transport.requests import Request

# Suppress file_cache warning
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# MongoDB setup with defaults
DB_NAME = os.environ.get('DB_NAME')
COLLECTION_NAME = os.environ.get('COLLECTION_NAME')
MONGO_CONNECTION_STRING = os.environ.get('MONGO_CONNECTION_STRING')
BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHANNEL_ID = os.environ.get('CHANNEL_ID')


client = pymongo.MongoClient(MONGO_CONNECTION_STRING)
db = client[DB_NAME]
collection = db[COLLECTION_NAME]

# Google Docs API setup
SCOPES = ['https://www.googleapis.com/auth/documents']
CREDS_FILE = 'credentials.json'  # Replace with your Google API credentials file path
TOKEN_FILE = 'token.json'

def get_google_docs_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            flow.redirect_uri = 'http://localhost:54391'
            creds = flow.run_local_server(port=54391)
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
    return build('docs', 'v1', credentials=creds)

def fetch_article_urls(base_url, pages):
    article_urls = []
    for page in range(1, pages + 1):
        url = base_url if page == 1 else f"{base_url}page/{page}/"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            for h1_tag in soup.find_all('h1', id='list'):
                a_tag = h1_tag.find('a')
                if a_tag and a_tag.get('href'):
                    article_urls.append(a_tag['href'])
        except requests.RequestException as e:
            logger.error(f"Failed to fetch URLs from {url}: {str(e)}")
    logger.info(f"Scraped {len(article_urls)} URLs: {article_urls}")
    return article_urls

def translate_to_gujarati(text):
    try:
        translator = GoogleTranslator(source='auto', target='gu')
        return translator.translate(text)
    except Exception as e:
        logger.error(f"Translation error: {str(e)}")
        return text

def download_and_convert_image(url):
    try:
        logger.info(f"Downloading image from {url}")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        content = response.content
        if len(content) < 100:
            logger.warning(f"Image at {url} is too small, likely invalid")
            return None
        img = Image.open(io.BytesIO(content))
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        img = img.resize((300, 225), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        img.save(output, format='PNG', optimize=True)
        output.seek(0)
        return output
    except Exception as e:
        logger.error(f"Failed to process image from {url}: {str(e)}")
        return None

async def scrape_and_get_content(url):
    try:
        logger.info(f"Starting to scrape content from {url}")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        logger.info(f"Content fetched for {url}")
        
        main_content = soup.find('div', class_='inside_post column content_width')
        if not main_content:
            raise Exception("Main content div not found")
        
        heading = main_content.find('h1', id='list')
        if not heading:
            raise Exception("Heading not found")
        
        image_div = soup.find('div', class_='featured_image')
        image_url = None
        if image_div:
            img_tag = image_div.find('img')
            if img_tag and img_tag.get('src'):
                image_url = img_tag['src']
        
        content_list = []
        heading_text = heading.get_text().strip()
        translated_heading = translate_to_gujarati(heading_text)
        content_list.append({'type': 'heading', 'text': translated_heading, 'image': image_url})
        content_list.append({'type': 'heading', 'text': heading_text, 'image': None})
        
        numbered_list_counter = 1
        category = None
        # Detect category from HTML within main_content
        category_tags = main_content.find_all('p', class_='small-font')
        for tag in category_tags:
            if tag.find('b', string='Category:'):
                category_link = tag.find('a', rel='tag')
                if category_link:
                    category = category_link.get_text().strip()
                    logger.info(f"Detected category from HTML for {url}: {category}")
                    break
        
        # Fallback: detect from plain text
        if not category:
            for tag in main_content.find_all(recursive=False):
                text = tag.get_text().strip()
                if not text:
                    continue
                category_match = re.search(r'Category: (.+)', text)
                if category_match:
                    category = category_match.group(1).strip()
                    logger.info(f"Detected category from text for {url}: {category}")
                    break
        
        for tag in main_content.find_all(recursive=False):
            if tag.get('class') in [['sharethis-inline-share-buttons', 'st-center', 'st-has-labels', 'st-inline-share-buttons', 'st-animated'], ['prenext']]:
                continue
            text = tag.get_text().strip()
            if not text or re.search(r'Category: (.+)', text):
                continue
            translated_text = translate_to_gujarati(text)
            if tag.name == 'p':
                content_list.append({'type': 'paragraph', 'text': translated_text})
                content_list.append({'type': 'paragraph', 'text': text})
            elif tag.name == 'h2':
                content_list.append({'type': 'heading_2', 'text': translated_text})
                content_list.append({'type': 'heading_2', 'text': text})
            elif tag.name == 'h4':
                content_list.append({'type': 'heading_4', 'text': translated_text})
                content_list.append({'type': 'heading_4', 'text': text})
            elif tag.name == 'ul':
                for li in tag.find_all('li'):
                    li_text = li.get_text().strip()
                    translated_li_text = translate_to_gujarati(li_text)
                    content_list.append({'type': 'bullet_list', 'text': translated_li_text})
                    content_list.append({'type': 'bullet_list', 'text': li_text})
            elif tag.name == 'ol':
                for li in tag.find_all('li'):
                    li_text = li.get_text().strip()
                    translated_li_text = translate_to_gujarati(li_text)
                    content_list.append({'type': 'numbered_list', 'text': translated_li_text, 'number': numbered_list_counter})
                    content_list.append({'type': 'numbered_list', 'text': li_text, 'number': numbered_list_counter})
                    numbered_list_counter += 1
        
        if not category:
            logger.warning(f"No category detected for {url}")
        logger.info(f"Finished scraping content for {url}")
        return content_list, category
    except Exception as e:
        logger.error(f"Error scraping {url}: {str(e)}")
        return [], None

def add_paragraph_border(paragraph):
    pPr = paragraph._element.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), '6')
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), 'C8C8C8')
    pBdr.append(bottom)
    pPr.append(pBdr)

def setup_document_styles(doc):
    styles = doc.styles
    title_style = styles.add_style('CoolTitle', WD_STYLE_TYPE.PARAGRAPH)
    title_style.font.name = 'Calibri'
    title_style.font.size = Pt(22)
    title_style.font.bold = True
    title_style.font.color.rgb = RGBColor(0, 102, 204)
    title_style.font.underline = True
    title_style.paragraph_format.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    title_style.paragraph_format.space_after = Pt(20)

    h1_style = styles.add_style('CoolHeading1', WD_STYLE_TYPE.PARAGRAPH)
    h1_style.font.name = 'Arial'
    h1_style.font.size = Pt(16)
    h1_style.font.bold = True
    h1_style.font.color.rgb = RGBColor(51, 51, 51)
    h1_style.paragraph_format.space_before = Pt(12)
    h1_style.paragraph_format.space_after = Pt(8)
    h1_style.paragraph_format.left_indent = Inches(0.25)

    h2_style = styles.add_style('CoolHeading2', WD_STYLE_TYPE.PARAGRAPH)
    h2_style.font.name = 'Arial'
    h2_style.font.size = Pt(14)
    h2_style.font.bold = True
    h2_style.font.italic = True
    h2_style.font.color.rgb = RGBColor(0, 153, 76)
    h2_style.paragraph_format.space_before = Pt(10)
    h2_style.paragraph_format.space_after = Pt(6)

    p_style = styles.add_style('CoolParagraph', WD_STYLE_TYPE.PARAGRAPH)
    p_style.font.name = 'Georgia'
    p_style.font.size = Pt(12)
    p_style.font.color.rgb = RGBColor(33, 33, 33)
    p_style.paragraph_format.line_spacing = 1.15
    p_style.paragraph_format.space_after = Pt(8)
    p_style.paragraph_format.first_line_indent = Inches(0.5)

    bullet_style = styles.add_style('CoolBulletList', WD_STYLE_TYPE.PARAGRAPH)
    bullet_style.font.name = 'Georgia'
    bullet_style.font.size = Pt(12)
    bullet_style.font.color.rgb = RGBColor(66, 66, 66)
    bullet_style.paragraph_format.left_indent = Inches(0.75)
    bullet_style.paragraph_format.first_line_indent = Inches(-0.25)
    bullet_style.paragraph_format.space_after = Pt(4)

    numbered_style = styles.add_style('CoolNumberedList', WD_STYLE_TYPE.PARAGRAPH)
    numbered_style.font.name = 'Georgia'
    numbered_style.font.size = Pt(12)
    numbered_style.font.color.rgb = RGBColor(66, 66, 66)
    numbered_style.paragraph_format.left_indent = Inches(0.75)
    numbered_style.paragraph_format.first_line_indent = Inches(-0.25)
    numbered_style.paragraph_format.space_after = Pt(4)

    img_style = styles.add_style('ImageParagraph', WD_STYLE_TYPE.PARAGRAPH)
    img_style.paragraph_format.alignment = WD_PARAGRAPH_ALIGNMENT.CENTER
    img_style.paragraph_format.space_before = Pt(6)
    img_style.paragraph_format.space_after = Pt(12)

def create_styled_document(content_list):
    doc = Document()
    setup_document_styles(doc)
    section = doc.sections[0]
    section.left_margin = Cm(2)
    section.right_margin = Cm(2)
    section.top_margin = Cm(1.5)
    section.bottom_margin = Cm(1.5)

    doc.add_paragraph(f"Current Affairs - {datetime.now().strftime('%d %B %Y')}", style='CoolTitle')
    
    for content in content_list:
        if content['type'] == 'heading':
            doc.add_paragraph(content['text'], style='CoolHeading1')
            if content.get('image'):
                image_data = download_and_convert_image(content['image'])
                if image_data:
                    p = doc.add_paragraph(style='ImageParagraph')
                    run = p.add_run()
                    run.add_picture(image_data, width=Inches(2.5), height=Inches(1.875))
        elif content['type'] == 'paragraph':
            p = doc.add_paragraph(content['text'], style='CoolParagraph')
            add_paragraph_border(p)
        elif content['type'] == 'heading_2':
            doc.add_paragraph(content['text'], style='CoolHeading2')
        elif content['type'] == 'heading_4':
            doc.add_paragraph(content['text'], style='Heading 4')
        elif content['type'] == 'bullet_list':
            doc.add_paragraph(f"• {content['text']}", style='CoolBulletList')
        elif content['type'] == 'numbered_list':
            doc.add_paragraph(f"{content['number']}. {content['text']}", style='CoolNumberedList')
        
        if content['type'] == 'heading' and content.get('image'):
            doc.add_section(WD_SECTION.CONTINUOUS)

    return doc

def append_to_google_doc(service, category, content_list):
    doc_title = f"{datetime.now().strftime('%B %Y')} - {category}"
    try:
        docs_service = service.documents()
        doc = docs_service.create(body={'title': doc_title}).execute()
        doc_id = doc['documentId']
        logger.info(f"Created new Google Doc: {doc_title} with ID {doc_id}")
        
        requests = []
        index = 1
        # Add title with styling
        requests.append({
            'insertText': {
                'location': {'index': index},
                'text': f"Current Affairs - {datetime.now().strftime('%d %B %Y')}\n"
            }
        })
        requests.append({
            'updateTextStyle': {
                'range': {'startIndex': 1, 'endIndex': len(f"Current Affairs - {datetime.now().strftime('%d %B %Y')}") + 1},
                'textStyle': {
                    'bold': True,
                    'foregroundColor': {'color': {'rgbColor': {'red': 0, 'green': 0.4, 'blue': 0.8}}},
                    'fontSize': {'magnitude': 18, 'unit': 'PT'}
                },
                'fields': '*'  # Specify all fields to update
            }
        })
        index += len(f"Current Affairs - {datetime.now().strftime('%d %B %Y')}") + 1
        
        # Add separator
        requests.append({
            'insertText': {
                'location': {'index': index},
                'text': '-' * 50 + '\n'
            }
        })
        index += 51  # Length of separator + newline
        
        for content in content_list:
            if content['type'] == 'heading':
                requests.append({
                    'insertText': {
                        'location': {'index': index},
                        'text': content['text'] + '\n'
                    }
                })
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': index, 'endIndex': index + len(content['text']) + 1},
                        'textStyle': {
                            'bold': True,
                            'foregroundColor': {'color': {'rgbColor': {'red': 0.2, 'green': 0.2, 'blue': 0.2}}},
                            'fontSize': {'magnitude': 14, 'unit': 'PT'}
                        },
                        'fields': '*'  # Specify all fields to update
                    }
                })
                index += len(content['text']) + 1
            elif content['type'] == 'paragraph':
                requests.append({
                    'insertText': {
                        'location': {'index': index},
                        'text': content['text'] + '\n\n'
                    }
                })
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': index, 'endIndex': index + len(content['text']) + 2},  # +2 for \n\n
                        'textStyle': {
                            'fontSize': {'magnitude': 11, 'unit': 'PT'},
                            'foregroundColor': {'color': {'rgbColor': {'red': 0.13, 'green': 0.13, 'blue': 0.13}}}
                        },
                        'fields': '*'  # Specify all fields to update
                    }
                })
                index += len(content['text']) + 2
            elif content['type'] == 'heading_2':
                requests.append({
                    'insertText': {
                        'location': {'index': index},
                        'text': content['text'] + '\n'
                    }
                })
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': index, 'endIndex': index + len(content['text']) + 1},
                        'textStyle': {
                            'bold': True,
                            'italic': True,
                            'foregroundColor': {'color': {'rgbColor': {'red': 0, 'green': 0.6, 'blue': 0.3}}},
                            'fontSize': {'magnitude': 12, 'unit': 'PT'}
                        },
                        'fields': '*'  # Specify all fields to update
                    }
                })
                index += len(content['text']) + 1
            elif content['type'] == 'heading_4':
                requests.append({
                    'insertText': {
                        'location': {'index': index},
                        'text': content['text'] + '\n'
                    }
                })
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': index, 'endIndex': index + len(content['text']) + 1},
                        'textStyle': {
                            'bold': True,
                            'foregroundColor': {'color': {'rgbColor': {'red': 0.4, 'green': 0.4, 'blue': 0.4}}},
                            'fontSize': {'magnitude': 11, 'unit': 'PT'}
                        },
                        'fields': '*'  # Specify all fields to update
                    }
                })
                index += len(content['text']) + 1
            elif content['type'] == 'bullet_list':
                requests.append({
                    'insertText': {
                        'location': {'index': index},
                        'text': f"• {content['text']}\n"
                    }
                })
                requests.append({
                    'createParagraphBullets': {
                        'range': {'startIndex': index, 'endIndex': index + len(f"• {content['text']}") + 1},
                        'bulletPreset': 'BULLET_DISC_CIRCLE_SQUARE'
                    }
                })
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': index, 'endIndex': index + len(f"• {content['text']}") + 1},
                        'textStyle': {
                            'fontSize': {'magnitude': 11, 'unit': 'PT'},
                            'foregroundColor': {'color': {'rgbColor': {'red': 0.26, 'green': 0.26, 'blue': 0.26}}}
                        },
                        'fields': '*'  # Specify all fields to update
                    }
                })
                index += len(f"• {content['text']}") + 1
            elif content['type'] == 'numbered_list':
                requests.append({
                    'insertText': {
                        'location': {'index': index},
                        'text': f"{content['number']}. {content['text']}\n"
                    }
                })
                requests.append({
                    'createParagraphBullets': {
                        'range': {'startIndex': index, 'endIndex': index + len(f"{content['number']}. {content['text']}") + 1},
                        'bulletPreset': 'NUMBERED_DECIMAL_ALPHA_ROMAN'
                    }
                })
                requests.append({
                    'updateTextStyle': {
                        'range': {'startIndex': index, 'endIndex': index + len(f"{content['number']}. {content['text']}") + 1},
                        'textStyle': {
                            'fontSize': {'magnitude': 11, 'unit': 'PT'},
                            'foregroundColor': {'color': {'rgbColor': {'red': 0.26, 'green': 0.26, 'blue': 0.26}}}
                        },
                        'fields': '*'  # Specify all fields to update
                    }
                })
                index += len(f"{content['number']}. {content['text']}") + 1
        
        # Add spacing and separator after each article
        requests.append({
            'insertText': {
                'location': {'index': index},
                'text': '\n' + '-' * 50 + '\n\n'
            }
        })
        index += 53  # Length of separator + newlines

        if requests:
            docs_service.batchUpdate(documentId=doc_id, body={'requests': requests}).execute()
            logger.info(f"Appended content to Google Doc: {doc_title} (ID: {doc_id})")
        else:
            logger.warning(f"No content to append to Google Doc: {doc_title}")
    except HttpError as e:
        logger.error(f"Failed to append to Google Doc {doc_title}: {str(e)}")

def check_and_insert_urls(urls):
    new_urls = []
    for url in urls:
        if 'daily-current-affairs-quiz' in url:
            continue
        if not collection.find_one({'url': url}):
            new_urls.append(url)
            collection.insert_one({'url': url, 'processed_date': datetime.now()})
    logger.info(f"Found {len(new_urls)} new URLs: {new_urls}")
    return new_urls

def send_docx_to_telegram(docx_path, bot_token, channel_id, caption):
    bot = telegram.Bot(token=bot_token)
    telegram_caption_limit = 1024
    file_size = os.path.getsize(docx_path) / 1024  # Size in KB
    logger.info(f"Attempting to send document of size {file_size:.2f} KB to Telegram")
    
    for attempt in range(5):
        try:
            with open(docx_path, 'rb') as docx_file:
                if len(caption) > telegram_caption_limit:
                    short_caption = caption[:telegram_caption_limit-3] + "..."
                    bot.send_document(
                        chat_id=channel_id,
                        document=docx_file,
                        filename=os.path.basename(docx_path),
                        caption=short_caption,
                        timeout=60
                    )
                    bot.send_message(chat_id=channel_id, text=caption, timeout=60)
                else:
                    bot.send_document(
                        chat_id=channel_id,
                        document=docx_file,
                        filename=os.path.basename(docx_path),
                        caption=caption,
                        timeout=60
                    )
            logger.info("Document sent successfully to Telegram")
            break
        except telegram.error.TimedOut as e:
            logger.warning(f"Telegram timeout on attempt {attempt + 1}, retrying... ({str(e)})")
            asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Failed to send document to Telegram: {str(e)}")
            raise
    else:
        logger.error("All retries failed to send document to Telegram")

async def main():
    try:
        base_url = "https://www.gktoday.in/current-affairs/"
        article_urls = fetch_article_urls(base_url, 3)
        if not article_urls:
            logger.warning("No URLs scraped. Check website structure or connectivity.")
            return
        
        new_urls = check_and_insert_urls(article_urls)
        if not new_urls:
            logger.warning("No new URLs to process")
            return
        
        all_content = []
        english_titles = []
        category_contents = {}
        for url in new_urls:
            content_list, category = await scrape_and_get_content(url)
            if content_list:
                all_content.extend(content_list)
                english_titles.append(content_list[1]['text'])
                if category:
                    if category not in category_contents:
                        category_contents[category] = []
                    category_contents[category].extend(content_list)
        
        if not all_content:
            logger.warning("No content scraped from new URLs")
            return
        
        logger.info(f"Categories detected: {list(category_contents.keys())}")
        
        doc = create_styled_document(all_content)
        current_date = datetime.now().strftime('%d-%m-%Y')
        docx_filename = f"{current_date}_Current_Affairs.docx"
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.docx') as tmp_docx:
            doc.save(tmp_docx.name)
            docx_path = tmp_docx.name
            logger.info(f"Document saved to: {docx_path}")
        
        bot_token = 'BOT_TOKEN'
        channel_id = 'CHANNEL_ID'
        
        caption = (
            f"🎗️ {datetime.now().strftime('%d %B %Y')} Current Affairs 🎗️\n\n"
            + '\n'.join([f"👉 {title}" for title in english_titles]) + '\n\n'
            + "🎉 Join us :- @gujtest 🎉"
        )
        
        logger.info("Starting Telegram send process")
        send_docx_to_telegram(docx_path, bot_token, channel_id, caption)
        
        logger.info("Starting Google Docs append process")
        service = get_google_docs_service()
        if not category_contents:
            logger.warning("No categories found to create Google Docs")
        else:
            for category, contents in category_contents.items():
                append_to_google_doc(service, category, contents)
        
        os.unlink(docx_path)
        logger.info("Temporary file deleted")
        
    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        raise

if __name__ == "__main__":
    asyncio.run(main())
