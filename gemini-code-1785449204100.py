import os
import sys
import re
import time
import urllib.parse
import requests
import html2text

# ==================== CONFIGURATION ====================
CONFLUENCE_URL = "https://your-domain.atlassian.net/wiki"  # Base URL
START_PAGE_IDS = ["123456789"]                             # List of top-level Page IDs
EMAIL = "your-email@example.com"                           # Confluence account email
API_TOKEN = "your-api-token-or-PAT"                        # API Token
BASE_OUTPUT_DIR = "confluence_hierarchy_export"           # Output root folder
# =======================================================


def sanitize_name(name):
    """Sanitizes names for filesystem safety while preserving readability."""
    sanitized = re.sub(r'[\\/*?:"<>|]', '', name).strip()
    return sanitized if sanitized else "Untitled_Page"


def fetch_paginated_api(url, auth):
    """Fetches all items across paginated Confluence REST API endpoints."""
    results = []
    current_url = url

    # Parse base domain for relative next links
    parsed_base = urllib.parse.urlparse(CONFLUENCE_URL)
    domain_base = f"{parsed_base.scheme}://{parsed_base.netloc}"

    while current_url:
        try:
            res = requests.get(current_url, auth=auth, timeout=15)
            if res.status_code != 200:
                print(f"  [!] API Warning (HTTP {res.status_code}): {res.text}")
                break

            data = res.json()
            results.extend(data.get('results', []))

            # Handle pagination link
            next_link = data.get('_links', {}).get('next')
            if next_link:
                current_url = f"{domain_base}{next_link}" if next_link.startswith('/') else next_link
                time.sleep(0.1)
            else:
                current_url = None

        except requests.exceptions.RequestException as e:
            print(f"  [!] API Fetch Error: {e}")
            break

    return results


def verify_connection(url, page_id, auth):
    """Pre-flight check for network connectivity, auth, and page existence."""
    api_url = f"{url}/rest/api/content/{page_id}?expand=body.storage"
    
    try:
        print(f"[+] Testing connection to: {url}...")
        res = requests.get(api_url, auth=auth, timeout=10)

        if res.status_code == 200:
            print("[✓] Connection & Authentication successful!\n")
            return True
        elif res.status_code == 401:
            print("[X] Auth Error (401): Check your Email and API Token.")
        elif res.status_code == 403:
            print("[X] Permission Error (403): You do not have access to view this page/space.")
        elif res.status_code == 404:
            print(f"[X] Not Found (404): Page ID '{page_id}' does not exist.")
        else:
            print(f"[X] HTTP Error {res.status_code}: {res.text}")
            
    except requests.exceptions.Timeout:
        print("[X] Connection Error: Request timed out. Check network connection.")
    except requests.exceptions.ConnectionError:
        print("[X] Connection Error: Failed to reach server. Verify base CONFLUENCE_URL.")

    return False


def preprocess_confluence_html(html_body):
    """Cleans up Confluence HTML, macro code blocks, and table breaks."""
    if not html_body:
        return ""

    # 1. Convert Confluence Code Macros to standard HTML <pre><code> blocks
    def code_macro_replacer(match):
        code_content = match.group(1)
        code_content = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', code_content, flags=re.DOTALL)
        return f"<pre><code>{code_content}</code></pre>"

    html_body = re.sub(
        r'<ac:structured-macro[^>]*ac:name="code"[^>]*>.*?<ac:plain-text-body>(.*?)</ac:plain-text-body>.*?</ac:structured-macro>',
        code_macro_replacer,
        html_body,
        flags=re.DOTALL
    )

    # 2. Prevent breaks inside table cells from ruining Markdown tables
    def clean_table_breaks(match):
        table_content = match.group(0)
        return re.sub(r'<br\s*/?>', ' ', table_content, flags=re.IGNORECASE)

    html_body = re.sub(r'<table.*?>.*?</table>', clean_table_breaks, html_body, flags=re.DOTALL | re.IGNORECASE)

    return html_body


def download_attachment_stream(download_url, auth, target_path):
    """Streams attachment download to disk."""
    try:
        with requests.get(download_url, auth=auth, stream=True, timeout=20) as r:
            if r.status_code == 200:
                with open(target_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                return True
            else:
                print(f"  [!] Failed to download attachment (HTTP {r.status_code})")
    except Exception as e:
        print(f"  [!] Error streaming attachment: {e}")
    return False


def process_images_and_attachments(url, page_id, auth, html_body, current_dir):
    """Downloads images and safely replaces Confluence XML image tags with standard HTML."""
    att_api_url = f"{url}/rest/api/content/{page_id}/child/attachment"
    attachments = fetch_paginated_api(att_api_url, auth)

    if not attachments:
        return html_body

    img_folder = os.path.join(current_dir, "images")
    os.makedirs(img_folder, exist_ok=True)

    print(f"  └─ Found {len(attachments)} attached file(s). Downloading...")

    # Extract base domain for proper attachment URL concatenation
    parsed_base = urllib.parse.urlparse(url)
    domain_base = f"{parsed_base.scheme}://{parsed_base.netloc}"

    for att in attachments:
        filename = att['title']
        download_rel_path = att['_links']['download']
        
        # Build the exact, correct download URL 
        download_url = f"{domain_base}{download_rel_path}" if download_rel_path.startswith('/') else f"{url.rstrip('/')}/{download_rel_path}"
        local_filepath = os.path.join(img_folder, filename)

        if download_attachment_stream(download_url, auth, local_filepath):
            encoded_filename = urllib.parse.quote(filename)
            
            # Use Regex to replace the entire Confluence <ac:image> XML wrapper with a standard HTML <img>
            escape_name = re.escape(filename)
            pattern = r'<ac:image[^>]*>.*?ri:filename="' + escape_name + r'".*?</ac:image>'
            html_img_tag = f'<img src="images/{encoded_filename}" width="500" alt="{filename}" />'
            
            html_body = re.sub(pattern, html_img_tag, html_body, flags=re.DOTALL | re.IGNORECASE)

    return html_body


def export_page_recursively(url, page_id, auth, parent_dir, depth=0):
    """Recursively processes a page and all its sub-pages."""
    indent = "  " * depth
    
    page_api_url = f"{url}/rest/api/content/{page_id}?expand=body.storage"
    try:
        res = requests.get(page_api_url, auth=auth, timeout=15)
        if res.status_code != 200:
            print(f"{indent}[X] Error reading Page ID {page_id} (HTTP {res.status_code})")
            return
        data = res.json()
    except Exception as e:
        print(f"{indent}[X] Network error fetching page {page_id}: {e}")
        return

    title = data.get('title', 'Untitled')
    # Default to empty string if body is empty or None
    raw_html = data.get('body', {}).get('storage', {}).get('value', '') or ''
    clean_title = sanitize_name(title)

    current_dir = os.path.join(parent_dir, clean_title)
    os.makedirs(current_dir, exist_ok=True)

    print(f"{indent}[+] ({page_id}) Exporting: '{title}'")

    preprocessed_html = preprocess_confluence_html(raw_html)
    updated_html = process_images_and_attachments(url, page_id, auth, preprocessed_html, current_dir)

    h = html2text.HTML2Text()
    h.body_width = 0           
    h.ignore_links = False     
    h.ignore_images = False    
    h.ignore_tables = False    

    md_content = h.handle(updated_html)

    md_filepath = os.path.join(current_dir, f"{clean_title}.md")
    with open(md_filepath, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n{md_content}")

    time.sleep(0.2)

    children_api_url = f"{url}/rest/api/content/{page_id}/child/page"
    child_pages = fetch_paginated_api(children_api_url, auth)

    if child_pages:
        print(f"{indent}  └─ Found {len(child_pages)} sub-page(s). Traversing deeper...")
        for child in child_pages:
            export_page_recursively(url, child['id'], auth, current_dir, depth + 1)


def main():
    auth = (EMAIL, API_TOKEN)
    
    if not START_PAGE_IDS or not verify_connection(CONFLUENCE_URL, START_PAGE_IDS[0], auth):
        sys.exit(1)

    os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
    print(f"[=== Starting Hierarchical Confluence Export ===]\n")

    for start_id in START_PAGE_IDS:
        export_page_recursively(CONFLUENCE_URL, start_id, auth, BASE_OUTPUT_DIR)

    print(f"\n[✓] Complete! All pages exported to: '{BASE_OUTPUT_DIR}'")


if __name__ == "__main__":
    main()