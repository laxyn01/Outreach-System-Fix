import re
import urllib.parse


def replace_placeholders(text: str, lead, sender_name: str, video_link: str = '') -> str:
    if not text:
        return text
    first = (getattr(lead, 'first_name', None) or 'there').strip() or 'there'
    last = (getattr(lead, 'last_name', None) or '').strip()
    company = (getattr(lead, 'company', None) or '').strip()
    # Bug fix #2: all placeholders including [Name] and [name]
    replacements = {
        '{first_name}': first,
        '{last_name}': last,
        '{company}': company or 'your company',
        '{sender_name}': sender_name or 'Your Name',
        '[Name]': first,
        '[name]': first,
        '{video_link}': video_link or '',
        '{pitch}': getattr(lead, 'pitch_text', '') or '',
    }
    for key, val in replacements.items():
        text = text.replace(key, val)
    return text


def wrap_links(html: str, lead_id: int, step: int, base_url: str) -> str:
    if not html:
        return html
    base_url = base_url.rstrip('/')

    def repl(match):
        url = match.group(1)
        if url.startswith(base_url) or url.startswith('#') or url.startswith('mailto:'):
            return match.group(0)
        encoded = urllib.parse.quote(url, safe='')
        return f'href="{base_url}/track/click/{lead_id}/{step}?url={encoded}"'

    return re.sub(r'href="([^"]+)"', repl, html)


def inject_tracking_pixel(html: str, token: str, base_url: str) -> str:
    """Inject a 1x1 pixel at the very top of <body>.
    Uses unique per-send token — no duplicate URLs for resent emails."""
    base_url = base_url.rstrip('/')
    pixel = (
        f'<img src="{base_url}/r/{token}.gif" '
        f'width="1" height="1" style="display:block;width:1px;height:1px;border:0;" '
        f'alt="" border="0">'
    )
    body_match = re.search(r'<body[^>]*>', html, re.IGNORECASE)
    if body_match:
        insert_pos = body_match.end()
        return html[:insert_pos] + pixel + html[insert_pos:]
    return pixel + html


def append_unsubscribe(body_text: str, body_html: str, lead_id: int, base_url: str):
    base_url = base_url.rstrip('/')
    unsub_url = f'{base_url}/unsubscribe/{lead_id}'
    text_footer = f'\n\nTo unsubscribe: {unsub_url}'
    html_footer = (
        f'<br><p style="font-size:11px;color:#999;margin-top:24px;font-family:sans-serif;">'
        f'<a href="{unsub_url}" style="color:#999;text-decoration:underline;">Unsubscribe</a></p>'
    )
    body_text = (body_text or '') + text_footer
    body_html = (body_html or '') + html_footer
    return body_text, body_html


def ensure_html_wrapper(body: str, is_html: bool) -> tuple:
    if is_html:
        if '<html' not in body.lower():
            # Bug fix #3: preserve newlines by converting to <br>, don't strip
            body_html = body.replace('\n', '<br>\n')
            body = f'<html><body style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#333;">{body_html}</body></html>'
        return '', body
    # Plain text: keep as-is, also create HTML version preserving whitespace
    plain = body
    body_escaped = body.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    html = (
        f'<html><body style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#333;">'
        f'<div style="white-space:pre-wrap;">{body_escaped}</div>'
        f'</body></html>'
    )
    return plain, html
