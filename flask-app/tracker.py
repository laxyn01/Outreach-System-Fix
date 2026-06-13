import re
import urllib.parse


def replace_placeholders(text: str, lead, sender_name: str, video_link: str = '') -> str:
    if not text:
        return text
    first = (lead.first_name or 'there').strip() or 'there'
    last = (lead.last_name or '').strip()
    company = (lead.company or 'your company').strip() or 'your company'
    replacements = {
        '{first_name}': first,
        '{last_name}': last,
        '{company}': company,
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


def inject_tracking_pixel(html: str, lead_id: int, step: int, base_url: str) -> str:
    base_url = base_url.rstrip('/')
    pixel = (
        f'<img src="{base_url}/track/open/{lead_id}/{step}" '
        f'width="1" height="1" style="display:none" alt="">'
    )
    if '</body>' in html.lower():
        return re.sub(r'</body>', pixel + '</body>', html, count=1, flags=re.IGNORECASE)
    return html + pixel


def append_unsubscribe(body_text: str, body_html: str, lead_id: int, base_url: str):
    base_url = base_url.rstrip('/')
    unsub_url = f'{base_url}/unsubscribe/{lead_id}'
    text_footer = f'\n\nTo unsubscribe reply STOP or click: {unsub_url}'
    html_footer = (
        f'<p style="font-size:12px;color:#888;margin-top:2em;">'
        f'<a href="{unsub_url}" style="color:#888;">Unsubscribe</a></p>'
    )
    body_text = (body_text or '') + text_footer
    body_html = (body_html or '') + html_footer
    return body_text, body_html


def ensure_html_wrapper(body: str, is_html: bool) -> tuple:
    if is_html:
        if '<html' not in body.lower():
            body = f'<html><body>{body}</body></html>'
        return '', body
    plain = body
    html = f'<html><body><pre style="font-family:sans-serif;white-space:pre-wrap;">{body}</pre></body></html>'
    return plain, html
