import random
import re


def parse_spintax(text: str) -> str:
    if not text:
        return text
    while '{' in text and '|' in text:
        text = re.sub(
            r'\{([^{}]+)\}',
            lambda m: random.choice(m.group(1).split('|')),
            text,
        )
    return text
